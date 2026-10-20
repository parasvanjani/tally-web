"""End-to-end tests: real gateway (uvicorn) + fake TallyPrime. Run: python -m unittest -v"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import jwt
import requests
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa

from gateway.app import create_app
from gateway.auth import Authenticator
from gateway.config import Settings
from gateway.tally import TallyClient, decode_response, parse_amount
from tests import fake_tally

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def settings(tally_port: int, **over) -> Settings:
    base = dict(
        tally_url=f"http://127.0.0.1:{tally_port}", default_company=fake_tally.COMPANY, bind="127.0.0.1",
        port=0, auth_mode="dev", cf_team_domain="", cf_aud="", dev_user="owner@sharma.in",
        viewers=["*@sharma.in"], posters=["owner@sharma.in"], allowed_voucher_types=["Receipt", "Payment", "Contra", "Journal"],
        backdate_days=7, max_range_days=92, cache_ttl=60,
        audit_log=Path(tempfile.mkdtemp()) / "audit.jsonl", web_dir=ROOT / "web")
    base.update(over)
    return Settings(**base)


class Server:
    def __init__(self, app):
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.server.should_exit = True
        self.thread.join(5)


class S(requests.Session):
    def __init__(self, base):
        super().__init__()
        self.base, self.trust_env = base, False

    def request(self, method, url, *a, **kw):
        return super().request(method, self.base + url, *a, **kw)


def post_headers(key=None):
    return {"X-Requested-With": "tally-web", "Idempotency-Key": key or uuid.uuid4().hex}


class ParsingTests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(parse_amount("-1500.00"), Decimal("-1500.00"))
        self.assertEqual(parse_amount("1,500.00 Dr"), Decimal("-1500.00"))
        self.assertEqual(parse_amount("₹ 12,000.50 Cr"), Decimal("12000.50"))
        self.assertEqual(parse_amount(""), 0)

    def test_decode_utf16_and_bad_refs(self):
        raw = "<A>x&#4;y&#10;&#x1F;z&amp;</A>".encode("utf-16")
        self.assertEqual(decode_response(raw), "<A>xy&#10;z&amp;</A>")

    def test_voucher_xml_escapes_and_signs(self):
        xml = TallyClient.build_voucher_xml("R & K <Exports>", "Receipt", date(2026, 10, 20), [
            {"ledger": "Mehta & Sons", "side": "Cr", "amount": Decimal("100")},
            {"ledger": "Cash", "side": "Dr", "amount": Decimal("100")}], "a</NARRATION><x>", "Mehta & Sons")
        self.assertIn("<SVCURRENTCOMPANY>R &amp; K &lt;Exports&gt;</SVCURRENTCOMPANY>", xml)
        self.assertIn("<LEDGERNAME>Mehta &amp; Sons</LEDGERNAME><ISDEEMEDPOSITIVE>No</ISDEEMEDPOSITIVE><AMOUNT>100.00</AMOUNT>", xml)
        self.assertIn("<LEDGERNAME>Cash</LEDGERNAME><ISDEEMEDPOSITIVE>Yes</ISDEEMEDPOSITIVE><AMOUNT>-100.00</AMOUNT>", xml)
        self.assertIn("a&lt;/NARRATION&gt;&lt;x&gt;", xml)
        self.assertIn("<DATE>20261020</DATE>", xml)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tally = fake_tally.start()
        cls.s = settings(cls.tally.server_address[1])
        cls.srv = Server(create_app(cls.s))
        cls.http = S(cls.srv.base)

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.tally.shutdown()

    def test_static_and_headers(self):
        r = self.http.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("manifest.webmanifest", r.text)
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])

    def test_me_and_companies(self):
        me = self.http.get("/api/me").json()
        self.assertEqual(me["role"], "poster")
        names = [c["name"] for c in self.http.get("/api/companies").json()["companies"]]
        self.assertIn("R & K Exports", names)

    def test_dashboard_uses_reserved_group_names(self):
        d = self.http.get("/api/dashboard").json()
        self.assertEqual(d["tiles"]["receivables"], {"amount": "125000.00", "side": "Dr"})
        self.assertEqual(d["tiles"]["payables"], {"amount": "64000.00", "side": "Cr"})
        self.assertEqual(d["tiles"]["bank"]["amount"], "812400.50")
        self.assertTrue(d["recent"])

    def test_ledgers_utf16_and_search(self):
        d = self.http.get("/api/ledgers", params={"q": "sundry"}).json()
        self.assertEqual({l["name"] for l in d["ledgers"]}, {"Mehta & Sons", "Gupta Suppliers"})
        sales = [l for l in self.http.get("/api/ledgers").json()["ledgers"] if l["name"] == "Sales"][0]
        self.assertEqual(sales["group"], "Sales Accounts")  # &#4; stripped

    def test_ledger_statement_effect(self):
        d = self.http.get("/api/ledger", params={"name": "hdfc bank", "from": "2026-10-01", "to": "2026-10-20"}).json()
        self.assertEqual(d["ledger"], "HDFC Bank")
        self.assertEqual(d["vouchers"][0]["effect"], {"amount": "25000.00", "side": "Dr"})
        self.assertEqual(self.http.get("/api/ledger", params={"name": "nope"}).status_code, 404)

    def test_range_limits(self):
        r = self.http.get("/api/daybook", params={"from": "2025-01-01", "to": "2026-10-20"})
        self.assertEqual(r.status_code, 400)
        r = self.http.get("/api/daybook", params={"from": "2026-10-21", "to": "2026-10-20"})
        self.assertEqual(r.status_code, 400)

    def _voucher(self, **over):
        body = {"type": "Receipt", "date": date.today().isoformat(), "narration": "Cash sale",
                "lines": [{"side": "Dr", "ledger": "Cash", "amount": "1500"},
                          {"side": "Cr", "ledger": "sales", "amount": "1500.00"}]}
        body.update(over)
        return body

    def test_post_voucher_happy_path_and_idempotency(self):
        key = uuid.uuid4().hex
        r = self.http.post("/api/vouchers", json=self._voucher(), headers=post_headers(key))
        self.assertEqual(r.status_code, 200, r.text)
        sent = fake_tally.RECEIVED[-1]
        self.assertIn("<LEDGERNAME>Sales</LEDGERNAME>", sent)  # canonical name used
        self.assertIn("[web: owner@sharma.in]", sent)          # who posted it, visible in Tally
        self.assertIn("<PARTYLEDGERNAME>Sales</PARTYLEDGERNAME>", sent)
        again = self.http.post("/api/vouchers", json=self._voucher(), headers=post_headers(key))
        self.assertEqual(again.status_code, 409)
        audit = self.s.audit_log.read_text()
        self.assertIn("voucher_created", audit)

    def test_post_validation(self):
        cases = [
            (self._voucher(type="Sales"), "Voucher type"),
            (self._voucher(date=(date.today() - timedelta(days=30)).isoformat()), "within the last"),
            (self._voucher(date=(date.today() + timedelta(days=1)).isoformat()), "within the last"),
            (self._voucher(lines=[{"side": "Dr", "ledger": "Cash", "amount": "10"},
                                  {"side": "Cr", "ledger": "Sales", "amount": "9"}]), "do not match"),
            (self._voucher(lines=[{"side": "Dr", "ledger": "Cash", "amount": "-10"},
                                  {"side": "Cr", "ledger": "Sales", "amount": "-10"}]), "positive"),
            (self._voucher(lines=[{"side": "Dr", "ledger": "Cash</LEDGERNAME>", "amount": "10"},
                                  {"side": "Cr", "ledger": "Sales", "amount": "10"}]), "Unknown ledger"),
        ]
        for body, expect in cases:
            r = self.http.post("/api/vouchers", json=body, headers=post_headers())
            self.assertEqual(r.status_code, 400, (body, r.text))
            self.assertIn(expect, r.json()["error"])

    def test_csrf_and_size_guards(self):
        r = self.http.post("/api/vouchers", json=self._voucher(), headers={"Idempotency-Key": uuid.uuid4().hex})
        self.assertEqual(r.status_code, 400)
        r = self.http.post("/api/vouchers", data="x=1", headers={**post_headers(),
                           "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status_code, 400)
        r = self.http.post("/api/vouchers", json={"narration": "x" * 40000}, headers=post_headers())
        self.assertEqual(r.status_code, 413)

    def test_tally_line_error_surfaces(self):
        # A ledger Tally itself rejects is reported as 422, and the key can be retried.
        app_s = settings(self.tally.server_address[1])
        tc = TallyClient(app_s.tally_url)
        with self.assertRaises(Exception) as cm:
            tc.create_voucher(fake_tally.COMPANY, "Receipt", date.today(),
                              [{"ledger": "Ghost", "side": "Dr", "amount": Decimal(1)},
                               {"ledger": "Cash", "side": "Cr", "amount": Decimal(1)}], "", None)
        self.assertIn("does not exist", str(cm.exception))


class ViewerAndDownTests(unittest.TestCase):
    def test_viewer_cannot_post_and_stranger_denied(self):
        tally = fake_tally.start()
        srv = Server(create_app(settings(tally.server_address[1], dev_user="clerk@sharma.in")))
        try:
            http = S(srv.base)
            self.assertEqual(http.get("/api/me").json()["role"], "viewer")
            r = http.post("/api/vouchers", json={}, headers=post_headers())
            self.assertEqual(r.status_code, 403)
        finally:
            srv.stop()
        srv = Server(create_app(settings(tally.server_address[1], dev_user="someone@gmail.com")))
        try:
            self.assertEqual(S(srv.base).get("/api/me").status_code, 403)
        finally:
            srv.stop(); tally.shutdown()

    def test_tally_down_is_503(self):
        srv = Server(create_app(settings(free_port())))
        try:
            r = S(srv.base).get("/api/companies")
            self.assertEqual(r.status_code, 503)
            self.assertIn("Could not reach TallyPrime", r.json()["error"])
        finally:
            srv.stop()


class _FakeJWK:
    def __init__(self, pub):
        self.pub = pub

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": self.pub})()


class CloudflareJwtTests(unittest.TestCase):
    def setUp(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.s = settings(9, auth_mode="cloudflare", cf_team_domain="acme.cloudflareaccess.com", cf_aud="AUD123")
        self.auth = Authenticator(self.s, jwk_client=_FakeJWK(self.key.public_key()))

    def token(self, **over):
        now = datetime.now(timezone.utc)
        claims = {"email": "owner@sharma.in", "aud": ["AUD123"], "iss": "https://acme.cloudflareaccess.com",
                  "iat": now, "exp": now + timedelta(minutes=10)}
        claims.update(over)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

    def test_valid_token(self):
        u = self.auth.identify({"cf-access-jwt-assertion": self.token()}, {})
        self.assertEqual(u, {"email": "owner@sharma.in", "role": "poster"})

    def test_rejections(self):
        from gateway.auth import AuthError
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bad = [
            {},  # no token
            {"cf-access-jwt-assertion": self.token(aud=["OTHER"])},
            {"cf-access-jwt-assertion": self.token(iss="https://evil.cloudflareaccess.com")},
            {"cf-access-jwt-assertion": self.token(exp=datetime.now(timezone.utc) - timedelta(hours=1))},
            {"cf-access-jwt-assertion": self.token(email="x@gmail.com")},
            {"cf-access-jwt-assertion": jwt.encode({"email": "owner@sharma.in", "aud": "AUD123"}, other, algorithm="RS256")},
            {"cf-access-jwt-assertion": jwt.encode({"email": "owner@sharma.in", "aud": "AUD123",
                                                    "iss": "https://acme.cloudflareaccess.com"}, "k", algorithm="HS256")},
        ]
        for h in bad:
            with self.assertRaises(AuthError, msg=str(h)):
                self.auth.identify(h, {})

    def test_dev_mode_refuses_public_bind(self):
        with self.assertRaises(SystemExit):
            settings(9, bind="0.0.0.0").validate()


if __name__ == "__main__":
    os.chdir(ROOT)
    unittest.main(verbosity=2)
