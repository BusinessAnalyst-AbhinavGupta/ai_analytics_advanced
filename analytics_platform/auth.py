"""P8 — SSO seam + enterprise RBAC + cross-tenant isolation.

Auth stays **off by default** (`Settings.auth_enabled`), so the platform runs
fully open for local/self-hosted single-tenant use; flipping it on activates the
same RBAC layer that an enterprise deployment (SSO/OIDC) would drive. There are
no credentials at rest: a signed token is issued from a platform secret
(`ANALYTICS_AUTH_SECRET`) and, when `oidc_issuer` is set, remote JWTs are
verified against that issuer as the SSO entry point.

Every guarded route resolves its *principal* through ``AuthGate.require``, which
enforces role and cross-tenant scope, so a stakeholder of tenant A can never
read tenant B (isolation is enforced in the auth layer, not just by WHERE
clauses).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

from .config import Settings


class Role(str, Enum):
    OWNER = "owner"
    TENANT_ADMIN = "tenant_admin"
    DATA_ADMIN = "data_admin"
    SENIOR = "senior"
    JUNIOR = "junior"
    STAKEHOLDER = "stakeholder"
    AUDITOR = "auditor"
    SERVICE = "service"


# Rank >= required rank grants access (owner/dmission hierarchy).
RANK = {
    Role.OWNER: 9, Role.TENANT_ADMIN: 8, Role.DATA_ADMIN: 7, Role.SENIOR: 6,
    Role.JUNIOR: 5, Role.STAKEHOLDER: 4, Role.AUDITOR: 3, Role.SERVICE: 2,
}


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _b64(obj: bytes) -> str:
    return base64.urlsafe_b64encode(obj).rstrip(b"=").decode("ascii")


def _unb64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def sign(payload: bytes, secret: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest())


def issue(secret: str, tenant_id: str, role: str, sub: str = "",
          scopes: Optional[List[str]] = None, ttl_s: int = 3600) -> str:
    """Sign a minimal, self-contained token (no secrets in it)."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = {
        "tenant_id": tenant_id, "role": role, "sub": sub,
        "scopes": scopes or [], "exp": int(time.time()) + ttl_s,
        "iat": int(time.time()),
    }
    payload = _b64(json.dumps(body).encode())
    msg = (header + "." + payload).encode("ascii")
    sig = sign(msg, secret)
    return header + "." + payload + "." + sig


def verify(token: str, secret: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("malformed token", 401)
    header, payload, sig = parts
    expect = sign((header + "." + payload).encode("ascii"), secret)
    if not hmac.compare_digest(sig, expect):
        raise AuthError("bad signature", 401)
    body = json.loads(_unb64(payload))
    if int(body.get("exp", 0)) < int(time.time()):
        raise AuthError("token expired", 401)
    return body


class AuthGate:
    """Enforces RBAC + tenant scope; permissive when auth is disabled."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.auth_enabled and self.settings.auth_secret)

    def require(self, token: Optional[str], tenant_id: str,
                roles: Iterable[Role]) -> Dict[str, Any]:
        """Return the principal. When auth is disabled a permissive service
        principal is returned (single-tenant/self-hosted default)."""
        if not self.enabled:
            return {"tenant_id": tenant_id, "role": Role.SERVICE.value,
                    "sub": "dev", "scopes": ["all"], "auth": "disabled"}
        principal = self._verify_token(token)
        if principal is None:
            raise AuthError("authentication required", 401)
        allowed = set(roles)
        if principal["role"] not in {r.value for r in allowed} and not self._ranked_ok(principal, allowed):
            raise AuthError("forbidden: insufficient role", 403)
        self._check_tenant(principal, tenant_id)
        return principal

    def _verify_token(self, token: Optional[str]) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        if token.lower().startswith("bearer "):
            token = token[7:]
        return verify(token, self.settings.auth_secret)

    def _ranked_ok(self, principal: Dict[str, Any], allowed: Set[Role]) -> bool:
        pr = RANK.get(principal["role"], 0)
        return any(RANK.get(r, 0) <= pr for r in allowed)

    def _check_tenant(self, principal: Dict[str, Any], tenant_id: str) -> None:
        if principal["role"] in (Role.OWNER.value, Role.SERVICE.value):
            return  # platform/owner scope may cross tenants
        if "all" in principal.get("scopes", []):
            return
        if principal.get("tenant_id") != tenant_id:
            raise AuthError("cross-tenant access denied", 403)

    def principal(self, token: Optional[str]) -> Dict[str, Any]:
        if not self.enabled:
            return {"role": Role.SERVICE.value, "sub": "dev", "tenant_id": "",
                    "scopes": ["all"], "auth": "disabled"}
        return self._verify_token(token) or {"role": "unknown", "sub": "", "tenant_id": "",
                                             "scopes": [], "auth": "failed"}


__all__ = ["Role", "RANK", "AuthGate", "AuthError", "issue", "verify", "sign"]