# Tally on the web — without Tally Cloud Access

A low-cost, secure way to use your TallyPrime (Edit Log) books from any browser or phone,
with encrypted off-site backups on Google Drive.

```
 Phone / laptop browser
        │  HTTPS, Google sign-in + MFA (Cloudflare Access, free ≤ 50 users)
        ▼
 Cloudflare edge ──── outbound-only tunnel (cloudflared, no open ports) ────┐
                                                                            ▼
                                       Windows PC or VM running TallyPrime Edit Log
                                       ├─ Tally Web Gateway  127.0.0.1:8787  (this repo)
                                       │     verifies the Access token again, role checks,
                                       │     validation, audit log, caching
                                       ├─ TallyPrime XML server 127.0.0.1:9000 (firewalled)
                                       └─ Tally backups ──► rclone crypt ──► Google Drive
```

## Why it's built this way

* **Google Drive holds backups, not live data.** Tally data files need database-style locking;
  a Drive sync folder gives none, and half-synced or corrupted files propagate. Drive is used as an
  encrypted, append-only off-site copy of *finished* Tally backups.
* **The only "API" is TallyPrime's own XML/JSON-over-HTTP server.** It needs TallyPrime running with
  the company loaded, handles one request at a time, and has no authentication — so it is never
  exposed. The gateway is the only thing that talks to it, on loopback.
* **Nothing listens on the internet.** cloudflared dials out to Cloudflare; Access enforces sign-in
  before a request reaches you; the gateway verifies the signed Access token itself as a second lock.
* **Edit Log stays intact.** Entries posted from the phone go through Tally's normal import path, so
  Edit Log records them; the narration is tagged `[web: user@email]` and the gateway writes its own
  audit log (`logs/audit.jsonl`) with who, what, when and from where.

## What you get

| Screen | What it does |
|---|---|
| Home | Cash, bank, receivables, payables + last 7 days of entries |
| Ledgers | Search all ledgers with closing balances; tap for a statement over any date range |
| Day Book | Vouchers for a day or range (max 92 days per query) |
| Entry | Post Receipt / Payment / Contra / Journal with live Dr = Cr check (posters only) |

Installable on Android and iPhone ("Add to Home Screen"), dark mode, works offline in read-only
mode from the last data it saw. Posting needs a live connection and Tally's confirmation.

## Setup

**1. TallyPrime** — F1 (Help) → Settings → Connectivity → Client/Server configuration →
*TallyPrime acts as: Both*, port `9000`. Set the company to load on startup. Turn on the Backup
Scheduler (Release 7.0+) to back up daily to `C:\TallyBackups`. Create a Tally user for daily work;
keep the admin password separate.

**2. Domain + Cloudflare** — add a domain you own to a free Cloudflare account (any cheap domain works).

**3. Gateway** — install Python 3.11+ on the Tally PC, then in an elevated PowerShell:
```powershell
powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1
notepad C:\TallyWeb\.env      # company name, CF_TEAM_DOMAIN, CF_AUD, VIEWERS, POSTERS
Start-ScheduledTask -TaskName TallyWebGateway
```

**4. Tunnel** — install cloudflared (`winget install Cloudflare.cloudflared`), then:
```powershell
cloudflared tunnel login
cloudflared tunnel create tally-web
cloudflared tunnel route dns tally-web books.yourdomain.com
# copy deploy\cloudflared-config.yml to C:\ProgramData\cloudflared\config.yml and edit it
cloudflared service install
```

**5. Access policy** — Zero Trust → Access → Applications → Add → Self-hosted, domain
`books.yourdomain.com`. Policy: *Allow* → emails = the same people as VIEWERS/POSTERS. Login method:
Google (or one-time PIN). Session duration 8–24h. Copy the **Application Audience (AUD) tag** into `.env`.
Optional hardening: require country = India, add a WARP device-posture rule for staff laptops.

**6. Encrypted Drive backups** — put `rclone.exe` in `C:\TallyWeb\rclone\`, then
`rclone config` → remote `gdrive` (Google Drive, scope **drive.file**) → remote `tallycrypt`
(type crypt, wrapping `gdrive:TallyBackups`). Save the crypt passwords in a password manager.
Schedule `scripts\backup-to-drive.ps1` daily (command in the script header). It copies only finished
files, never deletes from Drive except by age (90 days), and verifies each upload.

**Restore drill (do it once now):** `rclone copy tallycrypt:<PCNAME> D:\restore-test`, then in
TallyPrime use Alt+Y → Restore on a spare machine and open the company.

## Where to run it, and what it costs

| Option | Monthly cost | Notes |
|---|---|---|
| Existing office PC (recommended start) | ≈ ₹0 (+ domain ≈ ₹70) | Must stay on, logged in, Tally open. Add a UPS. |
| Your own Windows VM, e.g. AWS/Azure Mumbai, 2 vCPU / 4 GB | ≈ ₹2,500–4,500 (estimate; Windows licence is included in the hourly VM price — check current rates) | Tally permits hosting your own licence on your own cloud for your own business. Use auto-shutdown at night to save cost. |
| Tally's TallyPrime Cloud Access | from ≈ ₹600 / user | What you've chosen not to buy — listed for comparison. |

Cloudflare Tunnel + Access are free up to 50 users. Google Drive: the free 15 GB is usually enough.

**Licensing — check before rolling out:** Tally requires a TVU pack when TallyPrime is used virtually
through RDP or similar. This gateway uses Tally's published integration interface rather than remote
desktop, but confirm with your Tally partner that your licence covers it — especially if you also RDP
into a cloud VM to operate Tally itself.

## Security checklist

- [ ] Port 9000 blocked inbound (script does it); no RDP/port-forwarding on the router
- [ ] Access policy lists named emails only; MFA on those Google accounts
- [ ] `POSTERS` kept to the 1–2 people who should post; others are `VIEWERS`
- [ ] `C:\TallyWeb` readable only by Administrators/SYSTEM (script sets this)
- [ ] BitLocker on the Tally PC/VM disk
- [ ] Backup restore drill done; crypt passwords stored offline
- [ ] Review `logs\audit.jsonl` and Tally's Edit Log monthly

Phones cache the last-viewed reports for offline use. On a shared or lost phone, clear site data
for `books.yourdomain.com` and remove the user from the Access policy.

## Free extra you already pay for

With active TSS, TallyPrime's **Reports in Browser** lets Tally.NET users view standard reports on a
phone (view-only). It complements this app: use it for full Tally reports, this app for quick
balances, statements and posting entries.

## Development

```bash
pip install -r requirements.txt
python tests/fake_tally.py 9000        # stand-in for TallyPrime
AUTH_MODE=dev VIEWERS=you@x.com POSTERS=you@x.com DEV_USER=you@x.com \
TALLY_COMPANY="Sharma Traders Pvt Ltd" python -m gateway   # http://127.0.0.1:8787
python -m unittest -v                  # 18 end-to-end tests
```

Before going live, check against your real Tally once in dev mode: the dashboard tiles, one ledger
statement, and one small Journal entry (then delete it in Tally). TDL collection behaviour can vary
slightly between Tally releases; `gateway/tally.py` holds every request in one place.

To add more voucher types (e.g. Sales with inventory) extend `build_voucher_xml`; inventory
entries need `ALLINVENTORYENTRIES.LIST` and are deliberately left out of the first version.
