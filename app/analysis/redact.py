"""Secret redaction for exception text (analysis engine-local copy).

Exception strings travel far: they are logged, copied into `AnalysisVerdict.error`,
persisted in `attempt_reports.verdict` and rendered on the teacher dashboard. An
`httpx.HTTPStatusError` embeds the full request URL, so a credential sent as a
query parameter (`?key=...`) would leak down that whole chain. The Gemini client
sends its key in the `x-goog-api-key` header instead; this helper is the second
line of defence: every exception string is scrubbed before it is logged or stored.

Duplicated in `app/grading/llm_gemini.py`, `app/feedback/llm_gemini.py` and
`app/results/service.py` on purpose (no cross-engine import, like the retry code).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote

REDACTED = "[REDACTED]"
# A literal secret shorter than this is not replaced (it would shred ordinary
# words); real API keys are far longer.
MIN_SECRET_CHARS = 8

# `?key=...` / `&key=...` / `api_key=...` in a URL or a message.
_KEY_PARAM_RE = re.compile(r"(?i)((?:[?&]|\b)(?:api[_-]?key|key)=)[^&\s'\"<>]+")
# `x-goog-api-key: ...` (a dumped header).
_KEY_HEADER_RE = re.compile(r"(?i)(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+")
# Anything shaped like a Google API key.
_GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


def redact(text: object, secrets: Iterable[str | None] = ()) -> str:
    """`text` with credentials removed: the literal `secrets` and any `key=<...>`."""
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= MIN_SECRET_CHARS:
            out = out.replace(secret, REDACTED).replace(quote(secret, safe=""), REDACTED)
    out = _KEY_PARAM_RE.sub(lambda m: m.group(1) + REDACTED, out)
    out = _KEY_HEADER_RE.sub(lambda m: m.group(1) + REDACTED, out)
    return _GOOGLE_KEY_RE.sub(REDACTED, out)
