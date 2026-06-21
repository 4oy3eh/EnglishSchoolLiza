"""Teacher auth: a stateless HMAC-signed bearer token (Phase 10).

The platform has exactly one privileged role — the teacher — so we deliberately
avoid a user table (and its migration): a single shared password mints a
short-lived signed token, and every admin route requires it. The token is
`base64url(payload).base64url(sig)` where `payload = "{sub}:{exp}"` and `sig =
HMAC-SHA256(secret, payload)`; verification is constant-time and checks expiry.

This is intentionally minimal but real: the secret never leaves the server, the
client cannot forge a token without it, and tokens expire. `fastapi-users` (a
full user store) is the documented upgrade path if multiple teachers ever land.
"""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256

from app.core.logging import get_logger

log = get_logger(__name__)

_SUBJECT = "teacher"


class AuthError(Exception):
    """Login failed or a presented token is missing/invalid/expired."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


class TokenSigner:
    """Mint and verify the teacher's bearer token from a shared secret."""

    def __init__(self, secret: str, *, ttl_seconds: int) -> None:
        self._secret = secret.encode("utf-8")
        self._ttl = ttl_seconds

    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._secret, payload, sha256).digest()

    def mint(self, *, now: float | None = None) -> str:
        """Issue a fresh token valid for `ttl_seconds`."""
        exp = int((now if now is not None else time.time()) + self._ttl)
        payload = f"{_SUBJECT}:{exp}".encode()
        token = f"{_b64(payload)}.{_b64(self._sign(payload))}"
        log.info("admin token minted sub=%s exp=%d", _SUBJECT, exp)
        return token

    def verify(self, token: str, *, now: float | None = None) -> str:
        """Return the token subject, or raise `AuthError` if invalid/expired."""
        try:
            payload_b64, sig_b64 = token.split(".", 1)
            payload = _unb64(payload_b64)
            sig = _unb64(sig_b64)
        except Exception as exc:  # noqa: BLE001 - any decode failure is an auth failure
            raise AuthError("malformed token") from exc

        # Constant-time signature check before trusting any payload bytes.
        if not hmac.compare_digest(sig, self._sign(payload)):
            raise AuthError("bad signature")

        try:
            sub, exp_str = payload.decode("utf-8").split(":", 1)
            exp = int(exp_str)
        except ValueError as exc:
            raise AuthError("malformed payload") from exc
        if sub != _SUBJECT:
            raise AuthError("unknown subject")
        if (now if now is not None else time.time()) >= exp:
            raise AuthError("token expired")
        return sub


def verify_password(presented: str, expected: str) -> bool:
    """Constant-time password comparison (avoids a timing side-channel)."""
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
