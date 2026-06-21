"""One-off: transcribe the listening sample mp3s so we know what audio maps where.

Not part of the app engines (the real ASR seam is `app/ingestion/asr*.py`, which is
WhisperX-based but uninstallable on Python 3.14). This uses faster-whisper directly —
the same Whisper engine WhisperX wraps — to produce a readable transcript with
[mm:ss] segment timecodes next to each mp3, for human understanding before ingest.

Usage:
    .venv/Scripts/python.exe scripts/transcribe_assets.py [MODEL]
MODEL defaults to "small" (good CPU accuracy). Outputs <mp3>.transcript.txt.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MP3S = [
    ROOT / "assets" / "a2" / "a2-key-for-schools-listening-sample-test.mp3",
    ROOT / "assets" / "b1" / "Preliminary for Schools PB Sample Test.mp3",
]


def _ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def main() -> None:
    # Windows consoles default to cp1252; Whisper can emit any unicode (incl.
    # hallucinated CJK on music/silence), so make stdout lossless-printable.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    model_size = sys.argv[1] if len(sys.argv) > 1 else "small"
    from faster_whisper import WhisperModel

    print(f"loading faster-whisper model={model_size} (cpu/int8)...", flush=True)
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    for mp3 in MP3S:
        if not mp3.exists():
            print(f"SKIP missing {mp3}", flush=True)
            continue
        out = mp3.with_suffix(".transcript.txt")
        if out.exists():
            print(f"SKIP already transcribed {out.name}", flush=True)
            continue
        print(f"\n=== transcribing {mp3.name} ===", flush=True)
        t0 = time.monotonic()
        segments, info = model.transcribe(
            str(mp3), language="en", vad_filter=True, beam_size=5
        )

        lines: list[str] = [
            f"# {mp3.name}",
            f"# model={model_size}  detected_lang={info.language}  "
            f"duration={_ts(info.duration)}",
            "",
        ]
        for seg in segments:
            stamp = f"[{_ts(seg.start)}–{_ts(seg.end)}]"
            text = seg.text.strip()
            lines.append(f"{stamp} {text}")
            print(f"{stamp} {text}", flush=True)

        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(
            f"--- wrote {out.name} ({len(lines) - 3} segments) in "
            f"{time.monotonic() - t0:.0f}s ---",
            flush=True,
        )


if __name__ == "__main__":
    main()
