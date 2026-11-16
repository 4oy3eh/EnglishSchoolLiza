"""Secret redaction for exception text (results engine-local copy).

`AttemptReport.error` is what the teacher sees on a failed report, and the same
string is logged. An exception raised by an LLM client can carry a request URL or
a dumped header; nothing that reaches a log line or the `attempt_reports` table
may contain a credential. The Gemini clients send their key in a header (never in
the URL) — this is the second line of defence for everything results logs/stores.

Duplicated from `app/analysis/redact.py` on purpose (no cross-engine import of a
helper, like the retry code in the three Gemini clients). The literal secrets are
read from settings at call time, so a key rotated in `.env` is covered.
"""

from __future__ import annotations

import re
import traceback
from urllib.parse import quote

from app.core.config import settings

REDACTED = "[REDACTED]"
# A literal secret shorter than this is not replaced (it would shred ordinary
# words); real keys are far longer.
MIN_SECRET_CHARS = 8

_KEY_PARAM_RE = re.compile(r"(?i)((?:[?&]|\b)(?:api[_-]?key|key)=)[^&\s'\"<>]+")
_KEY_HEADER_RE = re.compile(r"(?i)(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+")
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


def _configured_secrets() -> tuple[str, ...]:
    """The credentials this process holds (never logged, only used to scrub)."""
    return tuple(
        value
        for value in (
            settings.gemini_api_key,
            settings.admin_token_secret,
            settings.minio_secret_key,
        )
        if isinstance(value, str) and len(value) >= MIN_SECRET_CHARS
    )


def redact_secrets(text: object) -> str:
    """`text` with credentials removed: configured secrets and any `key=<...>`."""
    out = str(text)
    for secret in _configured_secrets():
        out = out.replace(secret, REDACTED).replace(quote(secret, safe=""), REDACTED)
    out = _KEY_PARAM_RE.sub(lambda m: m.group(1) + REDACTED, out)
    out = _KEY_HEADER_RE.sub(lambda m: m.group(1) + REDACTED, out)
    return _GOOGLE_KEY_RE.sub(REDACTED, out)


def redacted_traceback(exc: BaseException) -> str:
    """The exception's traceback as text, scrubbed (for an ERROR log line)."""
    return redact_secrets(
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
    )
