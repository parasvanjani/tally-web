"""Tally Web Gateway — a small, locked-down web API + mobile PWA over TallyPrime.

Run:  python -m gateway            (reads .env)
Only binds to loopback; Cloudflare Tunnel (cloudflared) publishes it.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .auth import ROLE_POSTER, AuthError, Authenticator
from .config import Settings
from .tally import TallyClient, TallyError, TallyUnavailable

MAX_BODY = 32 * 1024
READ_LIMIT, WRITE_LIMIT = 120, 15  # requests per minute per user


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


# --------------------------------------------------------------------- helpers
class TTLCache:
    def __init__(self, ttl: int):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            self._d.pop(key, None)
            return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = (time.monotonic() + self.ttl, value)

    def clear_company(self, company: str):
        with self._lock:
            for k in [k for k in self._d if company in k]:
                del self._d[k]


class RateLimiter:
    def __init__(self):
        self._hits, self._lock = defaultdict(deque), threading.Lock()

    def allow(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] < now - 60:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True


class Audit:
    def __init__(self, path):
        self.path, self._lock = path, threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, **event):
        event["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def parse_day(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, f"Bad date '{value}', use YYYY-MM-DD")


# ------------------------------------------------------------------------ app
def create_app(settings: Settings | None = None, tally: TallyClient | None = None,
               authenticator: Authenticator | None = None) -> Starlette:
    s = settings or Settings.load()
    tc = tally or TallyClient(s.tally_url)
    authn = authenticator or Authenticator(s)
    cache, limiter, audit = TTLCache(s.cache_ttl), RateLimiter(), Audit(s.audit_log)
    seen_keys: dict[str, float] = {}
    seen_lock = threading.Lock()

    async def cached(key: tuple, fn, *args):
        hit = cache.get(key)
        if hit is not None:
            return hit
        value = await run_in_threadpool(fn, *args)
        cache.put(key, value)
        return value

    def company_of(req: Request) -> str:
        c = (req.query_params.get("company") or s.default_company).strip()
        if not c:
            raise ApiError(400, "No company selected")
        if len(c) > 200:
            raise ApiError(400, "Company name too long")
        return c

    def date_range(req: Request) -> tuple[date, date]:
        today = date.today()
        d_to = parse_day(req.query_params.get("to"), today)
        d_from = parse_day(req.query_params.get("from"), d_to)
        if d_from > d_to:
            raise ApiError(400, "'from' is after 'to'")
        if (d_to - d_from).days > s.max_range_days:
            raise ApiError(400, f"Date range is limited to {s.max_range_days} days")
        return d_from, d_to

    async def ledger_names(company: str) -> dict[str, str]:
        ledgers = await cached(("ledgers", company), tc.ledgers, company)
        return {l["name"].lower(): l["name"] for l in ledgers}

    # ------------------------------------------------------------ endpoints
    async def me(req: Request):
        u = req.state.user
        return {"email": u["email"], "role": u["role"], "default_company": s.default_company,
                "voucher_types": s.allowed_voucher_types if u["role"] == ROLE_POSTER else [],
                "backdate_days": s.backdate_days}

    async def companies(req: Request):
        return {"companies": await cached(("companies",), tc.companies)}

    async def dashboard(req: Request):
        company = company_of(req)
        groups = await cached(("groups", company), tc.groups, company)
        wanted = {"Cash-in-Hand": "cash", "Bank Accounts": "bank", "Bank OD A/c": "bank_od",
                  "Sundry Debtors": "receivables", "Sundry Creditors": "payables"}
        tiles = {}
        for g in groups:
            key = wanted.get(g["reserved"]) or wanted.get(g["name"])
            if key and key not in tiles:
                tiles[key] = g["closing"]
        today = date.today()
        recent = await cached(("daybook", company, today - timedelta(days=6), today),
                              tc.daybook, company, today - timedelta(days=6), today)
        return {"company": company, "tiles": tiles, "recent": recent[-15:][::-1],
                "as_of": datetime.now().isoformat(timespec="minutes")}

    async def ledgers(req: Request):
        company = company_of(req)
        rows = await cached(("ledgers", company), tc.ledgers, company)
        q = (req.query_params.get("q") or "").lower().strip()
        if q:
            rows = [r for r in rows if q in r["name"].lower() or q in r["group"].lower()]
        return {"company": company, "ledgers": rows[:500], "total": len(rows)}

    async def ledger_statement(req: Request):
        company = company_of(req)
        name = (req.query_params.get("name") or "").strip()
        names = await ledger_names(company)
        if name.lower() not in names:
            raise ApiError(404, "Unknown ledger")
        name = names[name.lower()]
        d_from, d_to = date_range(req)
        rows = await cached(("ledgervch", company, name, d_from, d_to),
                            tc.ledger_vouchers, company, name, d_from, d_to)
        return {"company": company, "ledger": name, "from": d_from.isoformat(),
                "to": d_to.isoformat(), "vouchers": rows}

    async def daybook(req: Request):
        company = company_of(req)
        d_from, d_to = date_range(req)
        rows = await cached(("daybook", company, d_from, d_to), tc.daybook, company, d_from, d_to)
        return {"company": company, "from": d_from.isoformat(), "to": d_to.isoformat(), "vouchers": rows}

    async def create_voucher(req: Request):
        user = req.state.user
        if user["role"] != ROLE_POSTER:
            raise ApiError(403, "Your account can view but not post entries")
        idem = req.headers.get("idempotency-key", "")
        if not (8 <= len(idem) <= 64):
            raise ApiError(400, "Idempotency-Key header required")
        with seen_lock:
            now = time.time()
            for k in [k for k, t in seen_keys.items() if t < now - 86400]:
                del seen_keys[k]
            if idem in seen_keys:
                raise ApiError(409, "This entry was already submitted")
            seen_keys[idem] = now
        try:
            body = await req.json()
            company = (body.get("company") or s.default_company).strip()
            vtype = body.get("type", "")
            vdate = parse_day(body.get("date"), date.today())
            narration = str(body.get("narration", "")).strip()[:500]
            raw_lines = body.get("lines") or []
            if vtype not in s.allowed_voucher_types:
                raise ApiError(400, f"Voucher type must be one of {', '.join(s.allowed_voucher_types)}")
            today = date.today()
            if vdate > today or vdate < today - timedelta(days=s.backdate_days):
                raise ApiError(400, f"Date must be within the last {s.backdate_days} days")
            if not (2 <= len(raw_lines) <= 20):
                raise ApiError(400, "An entry needs between 2 and 20 lines")
            names = await ledger_names(company)
            lines, dr, cr = [], Decimal("0"), Decimal("0")
            for ln in raw_lines:
                lname = names.get(str(ln.get("ledger", "")).strip().lower())
                if not lname:
                    raise ApiError(400, f"Unknown ledger '{ln.get('ledger')}'")
                side = ln.get("side")
                if side not in ("Dr", "Cr"):
                    raise ApiError(400, "Each line needs side Dr or Cr")
                try:
                    amt = Decimal(str(ln.get("amount"))).quantize(Decimal("0.01"))
                except InvalidOperation:
                    raise ApiError(400, "Amounts must be numbers")
                if not (Decimal("0") < amt < Decimal("1e11")):
                    raise ApiError(400, "Amounts must be positive")
                lines.append({"ledger": lname, "side": side, "amount": amt})
                dr += amt if side == "Dr" else 0
                cr += amt if side == "Cr" else 0
            if dr != cr:
                raise ApiError(400, f"Debits ({dr}) and credits ({cr}) do not match")
            party = next((l["ledger"] for l in lines
                          if l["side"] == ("Cr" if vtype == "Receipt" else "Dr")), None) \
                if vtype in ("Receipt", "Payment") else None
            tagged = f"{narration} [web: {user['email']}]".strip()
            result = await run_in_threadpool(tc.create_voucher, company, vtype, vdate, lines, tagged, party)
        except Exception:
            with seen_lock:  # let the user retry a failed submission
                seen_keys.pop(idem, None)
            raise
        cache.clear_company(company)
        audit.write(event="voucher_created", user=user["email"], company=company, type=vtype,
                    date=vdate.isoformat(), total=f"{dr:.2f}", lines=len(lines), tally=result)
        return {"ok": True, "result": result}

    async def healthz(req: Request):
        return {"ok": True}

    # --------------------------------------------------------- middleware
    class Guard(BaseHTTPMiddleware):
        async def dispatch(self, req: Request, call_next):
            path = req.url.path
            ip = req.headers.get("cf-connecting-ip", req.client.host if req.client else "")
            if path.startswith("/api/"):
                try:
                    req.state.user = authn.identify(req.headers, req.cookies)
                except AuthError as e:
                    audit.write(event="auth_denied", path=path, ip=ip, reason=str(e))
                    return JSONResponse({"error": "Not authorised"}, status_code=403)
                write = req.method not in ("GET", "HEAD")
                if write:
                    # CSRF: JSON + a custom header can't be sent cross-site without CORS (which we never grant).
                    if req.headers.get("x-requested-with") != "tally-web" or \
                            "application/json" not in req.headers.get("content-type", ""):
                        return JSONResponse({"error": "Bad request"}, status_code=400)
                    if int(req.headers.get("content-length") or 0) > MAX_BODY:
                        return JSONResponse({"error": "Request too large"}, status_code=413)
                if not limiter.allow(f"{req.state.user['email']}:{write}", WRITE_LIMIT if write else READ_LIMIT):
                    return JSONResponse({"error": "Too many requests, slow down"}, status_code=429)
            resp: Response = await call_next(req)
            if path.startswith("/api/"):
                resp.headers["Cache-Control"] = "no-store"
                audit.write(event="request", user=getattr(req.state, "user", {}).get("email"),
                            method=req.method, path=path, status=resp.status_code, ip=ip)
            resp.headers.update({
                "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                           "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                                           "base-uri 'none'; form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Strict-Transport-Security": "max-age=31536000",
            })
            return resp

    def json_route(fn):
        async def wrapper(req):
            return JSONResponse(await fn(req))
        return wrapper

    routes = [
        Route("/healthz", json_route(healthz)),
        Route("/api/me", json_route(me)),
        Route("/api/companies", json_route(companies)),
        Route("/api/dashboard", json_route(dashboard)),
        Route("/api/ledgers", json_route(ledgers)),
        Route("/api/ledger", json_route(ledger_statement)),
        Route("/api/daybook", json_route(daybook)),
        Route("/api/vouchers", json_route(create_voucher), methods=["POST"]),
        Mount("/", StaticFiles(directory=str(s.web_dir), html=True)),
    ]
    async def on_api_error(req, e):
        return JSONResponse({"error": e.message}, status_code=e.status)

    async def on_unavailable(req, e):
        return JSONResponse({"error": str(e)}, status_code=503)

    async def on_tally_error(req, e):
        return JSONResponse({"error": f"TallyPrime: {e}"}, status_code=422)

    async def on_bad_json(req, e):
        return JSONResponse({"error": "Body must be JSON"}, status_code=400)

    handlers = {ApiError: on_api_error, TallyUnavailable: on_unavailable,
                TallyError: on_tally_error, json.JSONDecodeError: on_bad_json}
    app = Starlette(routes=routes, middleware=[Middleware(Guard)], exception_handlers=handlers)
    app.state.settings = s
    return app


def new_idempotency_key() -> str:  # used by tests/tools
    return secrets.token_hex(16)
