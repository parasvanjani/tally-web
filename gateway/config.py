"""Configuration from environment variables or a .env file next to the project."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _list(name: str, default: str = "") -> list[str]:
    return [s.strip().lower() for s in os.environ.get(name, default).split(",") if s.strip()]


@dataclass
class Settings:
    tally_url: str
    default_company: str
    bind: str
    port: int
    auth_mode: str              # "cloudflare" (production) or "dev" (localhost only)
    cf_team_domain: str         # e.g. yourteam.cloudflareaccess.com
    cf_aud: str                 # Application Audience (AUD) tag
    dev_user: str
    viewers: list[str]
    posters: list[str]
    allowed_voucher_types: list[str]
    backdate_days: int
    max_range_days: int
    cache_ttl: int
    audit_log: Path
    web_dir: Path = field(default=ROOT / "web")

    @classmethod
    def load(cls) -> "Settings":
        _load_dotenv(ROOT / ".env")
        s = cls(
            tally_url=os.environ.get("TALLY_URL", "http://127.0.0.1:9000"),
            default_company=os.environ.get("TALLY_COMPANY", ""),
            bind=os.environ.get("BIND", "127.0.0.1"),
            port=int(os.environ.get("PORT", "8787")),
            auth_mode=os.environ.get("AUTH_MODE", "cloudflare").lower(),
            cf_team_domain=os.environ.get("CF_TEAM_DOMAIN", "").removeprefix("https://").rstrip("/"),
            cf_aud=os.environ.get("CF_AUD", ""),
            dev_user=os.environ.get("DEV_USER", "dev@localhost").lower(),
            viewers=_list("VIEWERS"),
            posters=_list("POSTERS"),
            allowed_voucher_types=[t.strip() for t in os.environ.get(
                "ALLOWED_VOUCHER_TYPES", "Receipt,Payment,Contra,Journal").split(",") if t.strip()],
            backdate_days=int(os.environ.get("BACKDATE_DAYS", "7")),
            max_range_days=int(os.environ.get("MAX_RANGE_DAYS", "92")),
            cache_ttl=int(os.environ.get("CACHE_TTL", "60")),
            audit_log=Path(os.environ.get("AUDIT_LOG", str(ROOT / "logs" / "audit.jsonl"))),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.auth_mode not in ("cloudflare", "dev"):
            raise SystemExit("AUTH_MODE must be 'cloudflare' or 'dev'")
        if self.auth_mode == "dev" and self.bind not in ("127.0.0.1", "localhost", "::1"):
            raise SystemExit("AUTH_MODE=dev is only allowed when BIND is a loopback address")
        if self.auth_mode == "cloudflare" and not (self.cf_team_domain and self.cf_aud):
            raise SystemExit("CF_TEAM_DOMAIN and CF_AUD are required when AUTH_MODE=cloudflare")
        if not self.viewers and not self.posters:
            raise SystemExit("Set VIEWERS and/or POSTERS (comma-separated emails or *@domain)")
