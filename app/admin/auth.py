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
import ipaddress
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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


# --------------------------------------------------------------------------- #
# Deployment guard: never serve the admin surface on the public dev defaults.
# --------------------------------------------------------------------------- #
# Headers a tunnel agent / reverse proxy adds. A browser talking to uvicorn directly
# never sends them, so their presence means the request was relayed — whatever the
# socket peer (the proxy itself, often 127.0.0.1) looks like.
FORWARDING_HEADERS = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-real-ip",
        "x-client-ip",
        "true-client-ip",
        "cf-connecting-ip",
        "via",
    }
)


def is_loopback_host(host: str | None) -> bool:
    """True only for a literal loopback IP (127.0.0.0/8, ::1, ::ffff:127.x.x.x)."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool((mapped or ip).is_loopback)


def is_local_request(client_host: str | None, header_names: Iterable[str]) -> bool:
    """The request was made on this very machine, not relayed from elsewhere.

    Both must hold: the peer address is loopback AND no proxy/tunnel forwarding
    header is present. The header check covers a tunnel agent whose forwarded
    address uvicorn did not (or was told not to) resolve into `request.client`,
    and a spoofed `X-Forwarded-For: 127.0.0.1` under `--forwarded-allow-ips=*`.
    It cannot see a relay that adds no header at all (a raw TCP tunnel, a bare
    reverse proxy) — which is why the dev defaults are additionally tolerated only
    in an explicitly declared dev/test environment (`insecure_defaults`).
    """
    if not is_loopback_host(client_host):
        return False
    return not any(name.lower() in FORWARDING_HEADERS for name in header_names)


def insecure_defaults(
    *,
    environment: str,
    teacher_password: str,
    admin_token_secret: str,
    dev_password: str,
    dev_secret: str,
    dev_environments: frozenset[str],
    local_request: bool = False,
) -> list[str]:
    """Names of the auth settings still on their public dev default — fail-closed.

    Empty only when both were overridden, OR when the dev defaults are in use in
    an explicitly declared dev/test `environment` **and** the request is local
    (`is_local_request`). `Settings.environment` defaults to a non-dev value, so
    "dev"/"test" can only come from the process env or `.env`: a deployment that
    never configured anything is refused instead of silently running on
    `change-me`. A non-empty result means the admin API must refuse to log in and
    must refuse every token: the default secret is public, so anyone could forge
    a teacher token with it.
    """
    bad: list[str] = []
    if teacher_password == dev_password:
        bad.append("TEACHER_PASSWORD")
    if admin_token_secret == dev_secret:
        bad.append("ADMIN_TOKEN_SECRET")
    if bad and local_request and environment.strip().lower() in dev_environments:
        return []
    return bad


# --------------------------------------------------------------------------- #
# Login brute-force guard.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LoginDecision:
    """Outcome of one `LoginRateLimiter.attempt`."""

    ok: bool  # the password was checked and is right
    retry_after: int = 0  # > 0: locked out — the password was NOT checked
    scope: str = ""  # which limit refused it: "ip" | "global"
    failures: int = 0  # this key's failures inside the window (after a wrong one)
    global_failures: int = 0  # all keys' failures inside the window


class LoginRateLimiter:
    """In-memory sliding-window limit on failed logins: per client key + global.

    `max_failures` wrong passwords from one key (client IP) inside
    `window_seconds` lock that key out until its oldest failure ages out; a good
    login clears the key. `global_max_failures` (None/0 = off) caps wrong
    passwords from ALL keys together and is the real brute-force bound: rotating
    IPs cannot buy more guesses, and the per-key limit can stay lenient enough
    that a teacher sharing one key with a class (a proxy/tunnel that hides client
    IPs) is not locked out by a couple of typos. While the global limit is hit
    every login is refused, right password included — the price of the bound; an
    already signed-in teacher keeps working (the token is not re-checked here).

    `attempt` does check + verify + record under one lock, so concurrent requests
    cannot all pass the check before any failure is counted. Keys with no failure
    left in the window are dropped on every call (nothing accumulates), and a
    locked-out attempt records nothing, so with a global limit at most
    `global_max_failures` entries exist at a time. Process-local on purpose
    (single uvicorn worker, like the finalize locks); a multi-worker deployment
    would need Redis. Thread-safe: FastAPI runs sync routes on a thread pool.
    """

    def __init__(
        self,
        *,
        max_failures: int,
        window_seconds: float,
        global_max_failures: int | None = None,
    ) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self.global_max_failures = global_max_failures or None
        self._failures: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._lock = threading.Lock()

    # -- internals (call with the lock held) -------------------------------- #
    def _sweep(self, now: float) -> None:
        """Age out old failures everywhere and drop the keys left empty."""
        for q in (self._global, *self._failures.values()):
            while q and now - q[0] >= self.window:
                q.popleft()
        for key in [k for k, q in self._failures.items() if not q]:
            del self._failures[key]

    def _wait(self, q: deque[float] | None, limit: int | None, now: float) -> int:
        if not limit or q is None or len(q) < limit:
            return 0
        # The lock lifts once the failure that completes the limit leaves the window.
        blocking = q[len(q) - limit]
        return max(1, int(self.window - (now - blocking)) + 1)

    def _locked(self, key: str, now: float) -> tuple[int, str]:
        self._sweep(now)
        wait = self._wait(self._failures.get(key), self.max_failures, now)
        if wait:
            return wait, "ip"
        wait = self._wait(self._global, self.global_max_failures, now)
        return (wait, "global") if wait else (0, "")

    def _record(self, key: str, now: float) -> int:
        q = self._failures.setdefault(key, deque())
        q.append(now)
        self._global.append(now)
        return len(q)

    # -- public ------------------------------------------------------------- #
    def attempt(
        self, key: str, verify: Callable[[], bool], *, now: float | None = None
    ) -> LoginDecision:
        """Atomically: refuse if locked out, else run `verify()` and record it.

        `verify` (a constant-time password compare) runs under the lock, and only
        when the key is not locked out. A wrong password is counted for the key
        and globally; a right one clears the key (never the global count — a
        teacher logging in must not hand a distributed guesser a fresh budget).
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            wait, scope = self._locked(key, now)
            if wait:
                return LoginDecision(
                    ok=False,
                    retry_after=wait,
                    scope=scope,
                    failures=len(self._failures.get(key, ())),
                    global_failures=len(self._global),
                )
            if verify():
                self._failures.pop(key, None)
                return LoginDecision(ok=True, global_failures=len(self._global))
            failures = self._record(key, now)
            return LoginDecision(ok=False, failures=failures, global_failures=len(self._global))

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        """Seconds until `key` may try again (its own or the global lock); 0 = free."""
        now = time.monotonic() if now is None else now
        with self._lock:
            return self._locked(key, now)[0]

    def record_failure(self, key: str, *, now: float | None = None) -> int:
        """Count one failed login; returns the key's failures now inside the window.

        Unconditional (no lockout check) — the login route uses `attempt`."""
        now = time.monotonic() if now is None else now
        with self._lock:
            self._sweep(now)
            return self._record(key, now)

    def tracked_keys(self) -> int:
        """How many keys currently hold a failure (observability / tests)."""
        with self._lock:
            return len(self._failures)

    def reset(self, key: str | None = None) -> None:
        """Forget `key`'s failures (after a good login), or everything when None."""
        with self._lock:
            if key is None:
                self._failures.clear()
                self._global.clear()
            else:
                self._failures.pop(key, None)
