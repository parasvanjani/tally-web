"""Thin, defensive client for the TallyPrime XML-over-HTTP interface.

TallyPrime must be running with "TallyPrime acts as: Server (or Both)" on a
port (default 9000) and the target company loaded. This module only ever
talks to Tally on the loopback address; it is never exposed directly.

Notes on Tally's XML conventions handled here:
  * Amounts: negative = Debit, positive = Credit.
  * Dates: YYYYMMDD in requests and responses.
  * Tally's HTTP server handles one request at a time, so calls are
    serialised with a lock and short-lived results are cached upstream.
  * Responses can arrive as UTF-8 or UTF-16 and sometimes contain control
    character references (e.g. &#4;) that strict XML parsers reject.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape

import requests
from defusedxml import ElementTree as ET


class TallyUnavailable(Exception):
    """Tally is not running, not in server mode, or no company is loaded."""


class TallyError(Exception):
    """Tally answered but rejected or failed the request."""


def x(value: str) -> str:
    """XML-escape text and attribute values (company/ledger names often contain &)."""
    return escape(str(value), {'"': "&quot;", "'": "&apos;"})


def tally_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def iso_date(s: str | None) -> str | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y%m%d", "%d-%b-%Y", "%d-%b-%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


_AMOUNT_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def parse_amount(s: str | None) -> Decimal:
    """Parse Tally amount strings: '-1500.00', '1,500.00 Dr', '₹ 12,000.50 Cr'."""
    if not s:
        return Decimal("0")
    s = s.strip()
    m = _AMOUNT_RE.search(s)
    if not m:
        return Decimal("0")
    try:
        val = Decimal(m.group(0).replace(",", ""))
    except InvalidOperation:
        return Decimal("0")
    lower = s.lower()
    if lower.endswith("dr") and val > 0:
        val = -val
    return val


def signed(amount: Decimal) -> dict:
    """Represent a Tally signed amount as {amount, side} for the UI."""
    if amount == 0:
        return {"amount": "0.00", "side": ""}
    return {"amount": f"{abs(amount):.2f}", "side": "Dr" if amount < 0 else "Cr"}


_CHARREF = re.compile(r"&#(x[0-9a-fA-F]+|\d+);")
_BAD_RAW = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _drop_invalid_ref(m: re.Match) -> str:
    ref = m.group(1)
    code = int(ref[1:], 16) if ref[0] in "xX" else int(ref)
    return m.group(0) if code in (9, 10, 13) or code >= 32 else ""


def decode_response(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif len(raw) > 1 and raw[1:2] == b"\x00":
        text = raw.decode("utf-16-le")
    else:
        text = raw.decode("utf-8", errors="replace")
    text = text.lstrip("﻿")
    text = _CHARREF.sub(_drop_invalid_ref, text)
    return _BAD_RAW.sub("", text)


def _txt(el, tag: str) -> str:
    found = el.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


@dataclass
class TallyClient:
    url: str = "http://127.0.0.1:9000"
    timeout: float = 30.0

    def __post_init__(self):
        self._lock = threading.Lock()
        self._session = requests.Session()
        # Never route loopback Tally traffic through a corporate proxy.
        self._session.trust_env = False

    # ---------------------------------------------------------------- transport
    def post(self, body: str) -> str:
        with self._lock:
            try:
                r = self._session.post(
                    self.url,
                    data=body.encode("utf-8"),
                    headers={"Content-Type": "text/xml; charset=utf-8"},
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                raise TallyUnavailable(
                    "Could not reach TallyPrime. Is it open, in server mode, with the company loaded?"
                ) from e
        if r.status_code != 200:
            raise TallyUnavailable(f"TallyPrime returned HTTP {r.status_code}")
        return decode_response(r.content)

    def query(self, body: str):
        text = self.post(body)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise TallyError(f"Unreadable response from TallyPrime: {e}") from e
        errors = [e.text.strip() for e in root.iter("LINEERROR") if e.text]
        if errors:
            raise TallyError("; ".join(errors))
        return root

    # ------------------------------------------------------------- envelopes
    @staticmethod
    def _collection(name: str, type_: str, fetch: str, company: str | None,
                    extra_vars: str = "", child_of: str | None = None,
                    filters: str | None = None, formulae: str = "") -> str:
        company_var = f"<SVCURRENTCOMPANY>{x(company)}</SVCURRENTCOMPANY>" if company else ""
        child = f"<CHILDOF>{x(child_of)}</CHILDOF>" if child_of else ""
        filt = f"<FILTERS>{filters}</FILTERS>" if filters else ""
        return (
            "<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
            f"<TYPE>Collection</TYPE><ID>{name}</ID></HEADER><BODY><DESC>"
            f"<STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>{company_var}{extra_vars}</STATICVARIABLES>"
            f"<TDL><TDLMESSAGE><COLLECTION NAME=\"{name}\" ISMODIFY=\"No\">"
            f"<TYPE>{type_}</TYPE>{child}<FETCH>{fetch}</FETCH>{filt}</COLLECTION>{formulae}"
            "</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>"
        )

    # --------------------------------------------------------------- reads
    def ping(self) -> bool:
        try:
            self.companies()
            return True
        except (TallyUnavailable, TallyError):
            return False

    def companies(self) -> list[dict]:
        root = self.query(self._collection("GWCompanies", "Company", "Name,StartingFrom,BooksFrom", None))
        out = []
        for c in root.iter("COMPANY"):
            name = c.get("NAME") or _txt(c, "NAME")
            if name:
                out.append({
                    "name": name,
                    "starting_from": iso_date(_txt(c, "STARTINGFROM")),
                    "books_from": iso_date(_txt(c, "BOOKSFROM")),
                })
        return out

    def ledgers(self, company: str) -> list[dict]:
        root = self.query(self._collection(
            "GWLedgers", "Ledger", "Name,Parent,OpeningBalance,ClosingBalance", company))
        out = []
        for l in root.iter("LEDGER"):
            name = l.get("NAME") or _txt(l, "NAME")
            if not name:
                continue
            out.append({
                "name": name,
                "group": _txt(l, "PARENT"),
                "opening": signed(parse_amount(_txt(l, "OPENINGBALANCE"))),
                "closing": signed(parse_amount(_txt(l, "CLOSINGBALANCE"))),
            })
        out.sort(key=lambda r: r["name"].lower())
        return out

    def groups(self, company: str) -> list[dict]:
        root = self.query(self._collection("GWGroups", "Group", "Name,Parent,ClosingBalance", company))
        out = []
        for g in root.iter("GROUP"):
            name = g.get("NAME") or _txt(g, "NAME")
            if not name:
                continue
            closing = parse_amount(_txt(g, "CLOSINGBALANCE"))
            out.append({
                "name": name,
                "reserved": g.get("RESERVEDNAME") or "",
                "parent": _txt(g, "PARENT"),
                "closing_raw": closing,
                "closing": signed(closing),
            })
        return out

    def _parse_vouchers(self, root, for_ledger: str | None = None) -> list[dict]:
        out = []
        for v in root.iter("VOUCHER"):
            entries = list(v.iter("ALLLEDGERENTRIES.LIST")) or list(v.iter("LEDGERENTRIES.LIST"))
            entries = [(_txt(le, "LEDGERNAME"), parse_amount(_txt(le, "AMOUNT"))) for le in entries]
            entries = [(n, a) for n, a in entries if n]
            lines = [{"ledger": n, **signed(a)} for n, a in entries]
            row = {
                "date": iso_date(_txt(v, "DATE")),
                "type": _txt(v, "VOUCHERTYPENAME") or v.get("VCHTYPE", ""),
                "number": _txt(v, "VOUCHERNUMBER"),
                "party": _txt(v, "PARTYLEDGERNAME"),
                "narration": _txt(v, "NARRATION"),
                "master_id": _txt(v, "MASTERID"),
                "lines": lines,
            }
            if for_ledger:
                own = [a for n, a in entries if n.lower() == for_ledger.lower()]
                row["effect"] = signed(sum(own, Decimal("0")))
            else:
                row["amount"] = f"{sum((a for _, a in entries if a > 0), Decimal('0')):.2f}"
            out.append(row)
        out.sort(key=lambda r: (r["date"] or "", r["number"]))
        return out

    _VCH_FETCH = ("Date,VoucherTypeName,VoucherNumber,PartyLedgerName,Narration,MasterID,"
                  "AllLedgerEntries.LedgerName,AllLedgerEntries.Amount")
    _DATE_FILTER = ("<SYSTEM TYPE=\"Formulae\" NAME=\"GWInRange\">"
                    "$Date &gt;= ##SVFromDate AND $Date &lt;= ##SVToDate</SYSTEM>")

    def _range_vars(self, d_from: date, d_to: date) -> str:
        return (f"<SVFROMDATE TYPE=\"Date\">{tally_date(d_from)}</SVFROMDATE>"
                f"<SVTODATE TYPE=\"Date\">{tally_date(d_to)}</SVTODATE>")

    def daybook(self, company: str, d_from: date, d_to: date) -> list[dict]:
        root = self.query(self._collection(
            "GWDayBook", "Voucher", self._VCH_FETCH, company,
            extra_vars=self._range_vars(d_from, d_to), filters="GWInRange",
            formulae=self._DATE_FILTER))
        return self._parse_vouchers(root)

    def ledger_vouchers(self, company: str, ledger: str, d_from: date, d_to: date) -> list[dict]:
        root = self.query(self._collection(
            "GWLedgerVch", "Vouchers : Ledger", self._VCH_FETCH, company,
            extra_vars=self._range_vars(d_from, d_to), child_of=ledger,
            filters="GWInRange", formulae=self._DATE_FILTER))
        return self._parse_vouchers(root, for_ledger=ledger)

    # --------------------------------------------------------------- writes
    @staticmethod
    def build_voucher_xml(company: str, vtype: str, d: date, lines: list[dict],
                          narration: str, party: str | None) -> str:
        """lines: [{ledger, side: 'Dr'|'Cr', amount: Decimal}] — already validated & balanced."""
        entries = []
        for ln in lines:
            amt = Decimal(ln["amount"])
            is_dr = ln["side"] == "Dr"
            entries.append(
                "<ALLLEDGERENTRIES.LIST>"
                f"<LEDGERNAME>{x(ln['ledger'])}</LEDGERNAME>"
                f"<ISDEEMEDPOSITIVE>{'Yes' if is_dr else 'No'}</ISDEEMEDPOSITIVE>"
                f"<AMOUNT>{-amt if is_dr else amt:.2f}</AMOUNT>"
                "</ALLLEDGERENTRIES.LIST>"
            )
        party_xml = f"<PARTYLEDGERNAME>{x(party)}</PARTYLEDGERNAME>" if party else ""
        return (
            "<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
            "<REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME><STATICVARIABLES>"
            f"<SVCURRENTCOMPANY>{x(company)}</SVCURRENTCOMPANY></STATICVARIABLES></REQUESTDESC>"
            "<REQUESTDATA><TALLYMESSAGE xmlns:UDF=\"TallyUDF\">"
            f"<VOUCHER VCHTYPE=\"{x(vtype)}\" ACTION=\"Create\">"
            f"<DATE>{tally_date(d)}</DATE><VOUCHERTYPENAME>{x(vtype)}</VOUCHERTYPENAME>"
            f"{party_xml}<NARRATION>{x(narration)}</NARRATION>{''.join(entries)}"
            "</VOUCHER></TALLYMESSAGE></REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>"
        )

    def create_voucher(self, company: str, vtype: str, d: date, lines: list[dict],
                       narration: str, party: str | None) -> dict:
        root = self.query(self.build_voucher_xml(company, vtype, d, lines, narration, party))
        result = {k.lower(): _txt(root, f".//{k}") for k in
                  ("CREATED", "ALTERED", "ERRORS", "EXCEPTIONS", "LASTVCHID")}
        if result.get("errors") not in ("", "0") or result.get("created") in ("", "0"):
            raise TallyError(f"TallyPrime did not create the voucher (response: {result})")
        return result
