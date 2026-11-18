"""Student access control for the share link (Phase 14): PIN lockout + session tokens.

The per-test share link stays the entry point, but picking a name is no longer
enough: the student types their 4-character classroom PIN (`RosterEntry.pin`), and
a successful start issues a random **session token** for that attempt. Every later
`/exam/attempts/{id}/...` call and the telemetry ingest carry it in the
`X-Attempt-Token` header; the server stores only its SHA-256
(`AttemptRepository.add_attempt_session`).

`PinRateLimiter` bounds online guessing WITHOUT letting honest typos shut a class
out (golden rule #3: a refused student's timer keeps running). A class usually sits
behind ONE school NAT address -- or, behind a tunnel without `--proxy-headers`,
everybody is 127.0.0.1 -- so nothing here may lock "an IP" for plain typos.

* **Per student (roster entry) -- the real per-target guard.** `entry_max_failures`
  (5) wrong PINs inside `window_seconds` (300 s) lock that entry for `lock_seconds`
  (300 s). While locked even the right PIN is refused, otherwise the lock would
  bound nothing. A right PIN clears the entry's failures. 5 guesses / 5 min against
  31^4 = 923 521 PINs makes guessing one student hopeless.
* **Per IP -- a *spray* guard, not a typo counter.** It tracks the DISTINCT students
  an IP failed on (within `ip_window_seconds`, 600 s) that are still *unresolved*,
  and counts only those unresolved for longer than `ip_grace_seconds` (120 s). A
  right PIN for student X from that IP resolves X (the child who mistyped and then
  got in) and additionally "forgives" the oldest other unresolved student, once per
  student per window (the child who first picked a classmate's name, then their
  own). When `ip_distinct_entries` (5) students have stayed unresolved past the
  grace, the IP is throttled: a short lock that doubles per strike -- 30 s, 60 s,
  120 s, 240 s, capped at 300 s -- and the IP's tally starts over; strikes are
  forgotten 600 s after the last one. While throttled every PIN from that IP is
  refused unchecked (429 + Retry-After).
  Why a grace period instead of a raw distinct count: when a whole class of ~30
  starts within a minute and many children mistype, 8-15 of them are legitimately
  "mid-retype" at once (simulated: a raw distinct count of 6 or 8 throttled almost
  every such class start), but each one fixes the PIN within seconds; a sprayed
  name never gets resolved. 120 s is several retype cycles of a slow 8-year-old.
  Why 5: what can honestly stay unresolved > 2 min is a child waiting for the
  teacher (lost card / locked out) -- 1-3 in a class -- while wrong-name tries are
  forgiven by the child's own success. Simulated 30 children on one IP, 1-2 (also
  2-3) typos each, starting within 20-120 s, 4 wrong-name tries and 2 children
  stuck 3 min: zero throttles in 2 700 runs; a sprayer walking the roster is
  throttled after ~2-3 min.
  Why a short escalating lock instead of a 5-minute one: the throttle hits the
  whole NAT, sprayer and class alike, so the first strike must cost honest
  children seconds, not minutes, while a persistent sprayer ends up locked 5 min
  out of every ~7.
  Why "forgive once per student" and not "any right PIN decays the counter": the
  latter would let a pupil who knows their own PIN launder unlimited spraying.
* **Per test -- a monitor, never a lock.** Wrong PINs per test in a sliding
  `test_window_seconds` (600 s). Crossing `test_alert_failures` (40: more than one
  per child of a full class; a normal start with 10-30 % typos produces ~5-15) logs
  one loud WARNING (re-armed once the count drops below)
  and `test_activity` reports it to the teacher dashboard with counts per student.
  A per-test LOCK would let anyone holding the link shut the whole class out.

Known residual risk (documented in docs/OPERATIONS.md): anyone with the link can
type 5 wrong PINs on a classmate's name and lock that one student for 5 minutes.
The teacher remedy is «Снять блокировки» (`clear_test`: every student + IP lock of
the test) or «Сгенерировать новый PIN» (`reset_entry`: that student).

The check, the compare and the failure count are ONE step under a lock
(`attempt`), so parallel requests cannot all slip past the check before a failure is
counted. Lock events are logged (WARNING, one line per event) after the lock is
released; PINs are never logged. In-memory and process-local on purpose (single
uvicorn worker, like the admin login limiter and the finalize locks); a multi-worker
deployment would need Redis. The process-wide instance is `get_pin_limiter()`
(tests swap `PIN_LIMITER`; the getter reads it live).
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from app.core.logging import get_logger

log = get_logger(__name__)

# The header the runner sends on every attempt call (and the telemetry ingest).
TOKEN_HEADER = "X-Attempt-Token"


def new_session_token() -> str:
    """A fresh, unguessable per-attempt session token (256 bits, URL-safe)."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class PinDecision:
    """Outcome of one `PinRateLimiter.attempt`."""

    ok: bool  # the PIN was checked and is right
    retry_after: int = 0  # > 0: refused by a lock / throttle (or this failure set one)
    scope: str = ""  # which guard refused it / was just triggered: "entry" | "ip"
    failures: int = 0  # the entry's wrong PINs inside the window (after this one)
    tries_left: int = 0  # wrong PINs the entry may still make before it locks
    just_locked: bool = False  # this failure triggered a lock (entry and/or IP)
    entry_locked: bool = False  # this failure locked the student
    ip_throttled: bool = False  # this failure throttled the IP


@dataclass
class PinActivity:
    """Per-test wrong-PIN picture for the teacher dashboard (`test_activity`)."""

    window_seconds: int
    threshold: int
    wrong_pins: int
    alert: bool
    per_entry: dict[str, int] = field(default_factory=dict)  # entry -> wrong PINs
    locked: dict[str, int] = field(default_factory=dict)  # entry -> seconds left
    throttled_ips: int = 0  # IPs seen failing on this test that are throttled now


class PinRateLimiter:
    """In-memory wrong-PIN guard: per-student lock, per-IP spray throttle, per-test monitor."""

    def __init__(
        self,
        *,
        entry_max_failures: int = 5,
        window_seconds: float = 300.0,
        lock_seconds: float = 300.0,
        ip_distinct_entries: int = 5,
        ip_grace_seconds: float = 120.0,
        ip_window_seconds: float = 600.0,
        ip_throttle_base_seconds: float = 30.0,
        ip_throttle_max_seconds: float = 300.0,
        test_window_seconds: float = 600.0,
        test_alert_failures: int = 40,
    ) -> None:
        self.entry_max_failures = entry_max_failures
        self.window = window_seconds
        self.lock_seconds = lock_seconds
        self.ip_distinct_entries = ip_distinct_entries
        self.ip_grace = ip_grace_seconds
        self.ip_window = ip_window_seconds
        self.ip_throttle_base = ip_throttle_base_seconds
        self.ip_throttle_max = ip_throttle_max_seconds
        self.test_window = test_window_seconds
        self.test_alert_failures = test_alert_failures
        # per student
        self._entry_failures: dict[str, deque[float]] = {}
        self._entry_locked: dict[str, float] = {}
        # per IP: unresolved failed entry -> (first, last) failure from that IP
        self._ip_unresolved: dict[str, dict[str, tuple[float, float]]] = {}
        self._ip_forgiven: dict[str, dict[str, float]] = {}  # entry -> when it forgave
        self._ip_locked: dict[str, float] = {}
        self._ip_strikes: dict[str, tuple[int, float]] = {}  # (strikes, last strike)
        # per test: (time, entry, ip) of every wrong PIN in the window
        self._test_failures: dict[str, deque[tuple[float, str, str]]] = {}
        self._test_alerting: set[str] = set()
        self._lock = threading.Lock()

    def _tables(self) -> tuple[dict, ...]:
        return (
            self._entry_failures,
            self._entry_locked,
            self._ip_unresolved,
            self._ip_forgiven,
            self._ip_locked,
            self._ip_strikes,
            self._test_failures,
        )

    # -- internals (call with the lock held) -------------------------------- #
    def _sweep(self, now: float) -> None:
        for q in self._entry_failures.values():
            while q and now - q[0] >= self.window:
                q.popleft()
        for key in [k for k, q in self._entry_failures.items() if not q]:
            del self._entry_failures[key]
        for table in (self._entry_locked, self._ip_locked):
            for key in [k for k, until in table.items() if until <= now]:
                del table[key]
        for ip, per in list(self._ip_unresolved.items()):
            for entry in [e for e, (_, last) in per.items() if now - last >= self.ip_window]:
                del per[entry]
            if not per:
                del self._ip_unresolved[ip]
        for ip, forgave in list(self._ip_forgiven.items()):
            for entry in [e for e, t in forgave.items() if now - t >= self.ip_window]:
                del forgave[entry]
            if not forgave:
                del self._ip_forgiven[ip]
        for ip in [i for i, (_, t) in self._ip_strikes.items() if now - t >= self.ip_window]:
            del self._ip_strikes[ip]
        for test_id, tq in list(self._test_failures.items()):
            while tq and now - tq[0][0] >= self.test_window:
                tq.popleft()
            if len(tq) < self.test_alert_failures:
                self._test_alerting.discard(test_id)
            if not tq:
                del self._test_failures[test_id]

    @staticmethod
    def _wait(table: dict[str, float], key: str, now: float) -> int:
        until = table.get(key)
        return max(1, int(until - now + 0.999)) if until and until > now else 0

    def _resolve(self, entry_id: str, ip: str, now: float) -> None:
        """A right PIN for `entry_id` from `ip`: that student is no longer suspect,
        and (once per window per student) one other unresolved student is forgiven."""
        self._entry_failures.pop(entry_id, None)
        unresolved = self._ip_unresolved.get(ip)
        if not unresolved:
            return
        unresolved.pop(entry_id, None)
        forgiven = self._ip_forgiven.setdefault(ip, {})
        if unresolved and entry_id not in forgiven:
            oldest = min(unresolved, key=lambda e: unresolved[e][0])
            del unresolved[oldest]
            forgiven[entry_id] = now
        if not unresolved:
            self._ip_unresolved.pop(ip, None)
        if not forgiven:
            self._ip_forgiven.pop(ip, None)

    # -- public ------------------------------------------------------------- #
    def attempt(
        self,
        entry_id: str,
        client_ip: str | None,
        verify: Callable[[], bool],
        *,
        test_id: str | None = None,
        now: float | None = None,
    ) -> PinDecision:
        """Atomically: refuse while locked / throttled, else run `verify()` and count."""
        now = time.monotonic() if now is None else now
        ip = client_ip or "?"
        events: list[tuple[str, tuple[object, ...]]] = []
        with self._lock:
            self._sweep(now)
            wait = self._wait(self._entry_locked, entry_id, now)
            if wait:
                return PinDecision(ok=False, retry_after=wait, scope="entry")
            wait = self._wait(self._ip_locked, ip, now)
            if wait:
                return PinDecision(ok=False, retry_after=wait, scope="ip")
            if verify():
                self._resolve(entry_id, ip, now)
                return PinDecision(ok=True)

            # -- wrong PIN: per student --------------------------------------- #
            q = self._entry_failures.setdefault(entry_id, deque())
            q.append(now)
            failures = len(q)
            entry_locked = failures >= self.entry_max_failures
            if entry_locked:
                self._entry_locked[entry_id] = now + self.lock_seconds
                self._entry_failures.pop(entry_id, None)
                events.append((
                    "PIN LOCK student entry=%s test=%s ip=%s failures=%d -> locked %ds",
                    (entry_id, test_id, ip, failures, int(self.lock_seconds)),
                ))

            # -- wrong PIN: per IP (distinct students left unresolved) -------- #
            unresolved = self._ip_unresolved.setdefault(ip, {})
            first = unresolved.get(entry_id, (now, now))[0]
            unresolved[entry_id] = (first, now)
            distinct = sum(1 for f, _ in unresolved.values() if now - f >= self.ip_grace)
            throttle = 0
            if distinct >= self.ip_distinct_entries:
                strikes = self._ip_strikes.get(ip, (0, now))[0] + 1
                throttle = int(
                    min(self.ip_throttle_base * 2 ** (strikes - 1), self.ip_throttle_max)
                )
                self._ip_strikes[ip] = (strikes, now)
                self._ip_locked[ip] = now + throttle
                self._ip_unresolved.pop(ip, None)  # needs fresh students to strike again
                events.append((
                    "PIN THROTTLE ip=%s test=%s students_unresolved_over_%ds=%d strike=%d "
                    "-> every PIN from this IP refused for %ds",
                    (ip, test_id, int(self.ip_grace), distinct, strikes, throttle),
                ))

            # -- wrong PIN: per-test monitor (never locks) -------------------- #
            if test_id is not None:
                tq = self._test_failures.setdefault(test_id, deque())
                tq.append((now, entry_id, ip))
                if len(tq) >= self.test_alert_failures and test_id not in self._test_alerting:
                    self._test_alerting.add(test_id)
                    events.append((
                        "PIN ALERT test=%s: %d wrong PINs in the last %d min "
                        "(threshold %d) -- see the teacher dashboard roster",
                        (test_id, len(tq), int(self.test_window // 60), self.test_alert_failures),
                    ))

        for msg, args in events:
            log.warning(msg, *args)
        scope = "entry" if entry_locked else "ip" if throttle else ""
        retry_after = max(int(self.lock_seconds) if entry_locked else 0, throttle)
        return PinDecision(
            ok=False,
            retry_after=retry_after,
            scope=scope,
            failures=failures,
            tries_left=0 if entry_locked else self.entry_max_failures - failures,
            just_locked=bool(scope),
            entry_locked=entry_locked,
            ip_throttled=bool(throttle),
        )

    def reset_entry(self, entry_id: str) -> None:
        """Forget one entry's failures and lift its lock (teacher issued a new PIN)."""
        with self._lock:
            self._entry_failures.pop(entry_id, None)
            self._entry_locked.pop(entry_id, None)

    def clear_test(
        self, test_id: str, entry_ids: Iterable[str], *, now: float | None = None
    ) -> tuple[int, int]:
        """Lift every PIN lock of one test («Снять блокировки»).

        Clears the given students' failures + locks, and the failures / throttle /
        strikes of every IP that failed on this test (or on one of these students).
        The per-test monitor is kept: it is a record of the last minutes, not a lock.
        Returns (students that were locked, IPs that were throttled).
        """
        now = time.monotonic() if now is None else now
        entries = set(entry_ids)
        with self._lock:
            self._sweep(now)
            ips = {ip for _, _, ip in self._test_failures.get(test_id, ())}
            ips |= {ip for ip, per in self._ip_unresolved.items() if entries & per.keys()}
            students = sum(1 for e in entries if e in self._entry_locked)
            ip_locks = sum(1 for ip in ips if ip in self._ip_locked)
            for e in entries:
                self._entry_failures.pop(e, None)
                self._entry_locked.pop(e, None)
            for ip in ips:
                for table in (
                    self._ip_unresolved,
                    self._ip_forgiven,
                    self._ip_locked,
                    self._ip_strikes,
                ):
                    table.pop(ip, None)
        return students, ip_locks

    def test_activity(
        self, test_id: str, entry_ids: Iterable[str], *, now: float | None = None
    ) -> PinActivity:
        """Wrong PINs for one test in the monitor window + current locks (dashboard)."""
        now = time.monotonic() if now is None else now
        entries = set(entry_ids)
        with self._lock:
            self._sweep(now)
            tq = list(self._test_failures.get(test_id, ()))
            per_entry: dict[str, int] = {}
            for _, e, _ in tq:
                per_entry[e] = per_entry.get(e, 0) + 1
            locked = {e: w for e in entries if (w := self._wait(self._entry_locked, e, now))}
            throttled = sum(
                1 for ip in {i for _, _, i in tq} if self._wait(self._ip_locked, ip, now)
            )
        return PinActivity(
            window_seconds=int(self.test_window),
            threshold=self.test_alert_failures,
            wrong_pins=len(tq),
            alert=len(tq) >= self.test_alert_failures,
            per_entry=per_entry,
            locked=locked,
            throttled_ips=throttled,
        )

    def reset(self) -> None:
        """Forget everything (tests)."""
        with self._lock:
            for table in self._tables():
                table.clear()
            self._test_alerting.clear()

    def tracked_keys(self) -> int:
        """Keys holding a failure, a lock or a record (observability / tests)."""
        with self._lock:
            return sum(len(t) for t in self._tables())


# Process-wide limiter (single uvicorn worker). The student start route and the
# teacher remedies («Снять блокировки», «Сгенерировать новый PIN») share it through
# `get_pin_limiter()`; tests may swap `PIN_LIMITER` (the getter reads it live).
PIN_LIMITER = PinRateLimiter()


def get_pin_limiter() -> PinRateLimiter:
    """The process-wide `PinRateLimiter`."""
    return PIN_LIMITER
