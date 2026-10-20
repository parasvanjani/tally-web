"""Identity: verify the Cloudflare Access JWT on every request, then map email -> role.

Cloudflare Access already blocks unauthenticated users at the edge, but the
gateway re-verifies the signed token itself (defence in depth): if the tunnel
were ever misconfigured or someone reached the port locally, requests without
a valid, correctly-scoped token are still refused.
"""
from __future__ import annotations

import fnmatch

import jwt
from jwt import PyJWKClient

from .config import Settings

ROLE_POSTER = "poster"
ROLE_VIEWER = "viewer"


class AuthError(Exception):
    pass


def _matches(email: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(email, p) for p in patterns)


def role_for(email: str, s: Settings) -> str | None:
    email = email.lower()
    if _matches(email, s.posters):
        return ROLE_POSTER
    if _matches(email, s.viewers):
        return ROLE_VIEWER
    return None


class Authenticator:
    def __init__(self, s: Settings, jwk_client: PyJWKClient | None = None):
        self.s = s
        self._jwks = jwk_client
        if s.auth_mode == "cloudflare" and self._jwks is None:
            self._jwks = PyJWKClient(f"https://{s.cf_team_domain}/cdn-cgi/access/certs",
                                     cache_keys=True, lifespan=3600)

    def identify(self, headers, cookies) -> dict:
        if self.s.auth_mode == "dev":
            email = self.s.dev_user
        else:
            token = headers.get("cf-access-jwt-assertion") or cookies.get("CF_Authorization")
            if not token:
                raise AuthError("Missing Cloudflare Access token")
            try:
                key = self._jwks.get_signing_key_from_jwt(token).key
                claims = jwt.decode(
                    token, key, algorithms=["RS256"], audience=self.s.cf_aud,
                    issuer=f"https://{self.s.cf_team_domain}",
                    options={"require": ["exp", "iat", "aud", "iss"]}, leeway=30,
                )
            except Exception as e:  # noqa: BLE001 — any failure is an auth failure
                raise AuthError("Invalid Cloudflare Access token") from e
            email = (claims.get("email") or "").lower()
            if not email:
                raise AuthError("Token has no email (service tokens are not allowed)")
        role = role_for(email, self.s)
        if role is None:
            raise AuthError(f"{email} is not on the access list")
        return {"email": email, "role": role}
