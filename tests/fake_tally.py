"""A stand-in for TallyPrime's XML HTTP server, returning responses shaped like Tally's.

Run directly for UI development without Tally:  python tests/fake_tally.py  (listens on 127.0.0.1:9000)
"""
from __future__ import annotations

import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COMPANY = "Sharma Traders Pvt Ltd"
RECEIVED: list[str] = []

COMPANIES = f"""<ENVELOPE><BODY><DATA><COLLECTION>
<COMPANY NAME="{COMPANY.replace('&', '&amp;')}" RESERVEDNAME=""><STARTINGFROM TYPE="Date">20260401</STARTINGFROM><BOOKSFROM TYPE="Date">20260401</BOOKSFROM></COMPANY>
<COMPANY NAME="R &amp; K Exports" RESERVEDNAME=""><STARTINGFROM TYPE="Date">20260401</STARTINGFROM></COMPANY>
</COLLECTION></DATA></BODY></ENVELOPE>"""

LEDGERS = """<ENVELOPE><BODY><DATA><COLLECTION>
<LEDGER NAME="Cash" RESERVEDNAME="Cash"><PARENT TYPE="String">Cash-in-Hand</PARENT><OPENINGBALANCE TYPE="Amount">-10000.00</OPENINGBALANCE><CLOSINGBALANCE TYPE="Amount">-42350.00</CLOSINGBALANCE></LEDGER>
<LEDGER NAME="HDFC Bank" RESERVEDNAME=""><PARENT TYPE="String">Bank Accounts</PARENT><CLOSINGBALANCE TYPE="Amount">-812400.50</CLOSINGBALANCE></LEDGER>
<LEDGER NAME="Mehta &amp; Sons" RESERVEDNAME=""><PARENT TYPE="String">Sundry Debtors</PARENT><CLOSINGBALANCE TYPE="Amount">-125000.00</CLOSINGBALANCE></LEDGER>
<LEDGER NAME="Gupta Suppliers" RESERVEDNAME=""><PARENT TYPE="String">Sundry Creditors</PARENT><CLOSINGBALANCE TYPE="Amount">64000.00</CLOSINGBALANCE></LEDGER>
<LEDGER NAME="Office Rent" RESERVEDNAME=""><PARENT TYPE="String">Indirect Expenses</PARENT><CLOSINGBALANCE TYPE="Amount">-90000.00</CLOSINGBALANCE></LEDGER>
<LEDGER NAME="Sales" RESERVEDNAME=""><PARENT TYPE="String">Sales Accounts&#4;</PARENT><CLOSINGBALANCE TYPE="Amount">1500000.00</CLOSINGBALANCE></LEDGER>
</COLLECTION></DATA></BODY></ENVELOPE>"""

GROUPS = """<ENVELOPE><BODY><DATA><COLLECTION>
<GROUP NAME="Cash-in-Hand" RESERVEDNAME="Cash-in-Hand"><PARENT>Current Assets</PARENT><CLOSINGBALANCE>-42350.00</CLOSINGBALANCE></GROUP>
<GROUP NAME="Bank Accounts" RESERVEDNAME="Bank Accounts"><PARENT>Current Assets</PARENT><CLOSINGBALANCE>-812400.50</CLOSINGBALANCE></GROUP>
<GROUP NAME="Debtors" RESERVEDNAME="Sundry Debtors"><PARENT>Current Assets</PARENT><CLOSINGBALANCE>-125000.00</CLOSINGBALANCE></GROUP>
<GROUP NAME="Sundry Creditors" RESERVEDNAME="Sundry Creditors"><PARENT>Current Liabilities</PARENT><CLOSINGBALANCE>64000.00</CLOSINGBALANCE></GROUP>
</COLLECTION></DATA></BODY></ENVELOPE>"""

VOUCHERS = """<ENVELOPE><BODY><DATA><COLLECTION>
<VOUCHER VCHTYPE="Receipt"><DATE>{d}</DATE><VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME><VOUCHERNUMBER>12</VOUCHERNUMBER>
 <PARTYLEDGERNAME>Mehta &amp; Sons</PARTYLEDGERNAME><NARRATION>Part payment inv 88</NARRATION><MASTERID>101</MASTERID>
 <ALLLEDGERENTRIES.LIST><LEDGERNAME>Mehta &amp; Sons</LEDGERNAME><AMOUNT>25000.00</AMOUNT></ALLLEDGERENTRIES.LIST>
 <ALLLEDGERENTRIES.LIST><LEDGERNAME>HDFC Bank</LEDGERNAME><AMOUNT>-25000.00</AMOUNT></ALLLEDGERENTRIES.LIST></VOUCHER>
<VOUCHER VCHTYPE="Payment"><DATE>{d}</DATE><VOUCHERTYPENAME>Payment</VOUCHERTYPENAME><VOUCHERNUMBER>7</VOUCHERNUMBER>
 <PARTYLEDGERNAME>Office Rent</PARTYLEDGERNAME><NARRATION>October rent</NARRATION><MASTERID>102</MASTERID>
 <ALLLEDGERENTRIES.LIST><LEDGERNAME>Office Rent</LEDGERNAME><AMOUNT>-30000.00</AMOUNT></ALLLEDGERENTRIES.LIST>
 <ALLLEDGERENTRIES.LIST><LEDGERNAME>Cash</LEDGERNAME><AMOUNT>30000.00</AMOUNT></ALLLEDGERENTRIES.LIST></VOUCHER>
</COLLECTION></DATA></BODY></ENVELOPE>"""

CREATED = """<RESPONSE><CREATED>1</CREATED><ALTERED>0</ALTERED><DELETED>0</DELETED><LASTVCHID>555</LASTVCHID>
<LASTMID>0</LASTMID><COMBINED>0</COMBINED><IGNORED>0</IGNORED><ERRORS>0</ERRORS><CANCELLED>0</CANCELLED><EXCEPTIONS>0</EXCEPTIONS></RESPONSE>"""

LINEERROR = """<RESPONSE><LINEERROR>Ledger 'Ghost' does not exist!</LINEERROR><CREATED>0</CREATED><ERRORS>1</ERRORS></RESPONSE>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0))).decode("utf-8")
        RECEIVED.append(body)
        utf16 = False
        if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in body:
            out = LINEERROR if "Ghost" in body else CREATED
        else:
            m = re.search(r"<ID>(\w+)</ID>", body)
            ident = m.group(1) if m else ""
            from_d = re.search(r"<SVFROMDATE[^>]*>(\d+)<", body)
            out = {"GWCompanies": COMPANIES, "GWLedgers": LEDGERS, "GWGroups": GROUPS}.get(ident)
            if ident in ("GWDayBook", "GWLedgerVch"):
                out = VOUCHERS.format(d=from_d.group(1) if from_d else "20261020")
            utf16 = ident == "GWLedgers"  # exercise Tally's UTF-16 responses
            if out is None:
                out = "<RESPONSE>Unknown Request, cannot be processed</RESPONSE>"
        data = out.encode("utf-16") if utf16 else out.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=" + ("utf-16" if utf16 else "utf-8"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start(port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    print(f"Fake TallyPrime on http://127.0.0.1:{port}  (company: {COMPANY})")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
