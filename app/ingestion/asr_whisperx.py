"""Real `Asr` backed by WhisperX (transcription + word alignment + diarization).

Kept out of `app/ingestion/__init__.py` and importing WhisperX lazily, so the engine
has no hard runtime dependency on it — tests use `MockAsr` and only this module pulls
the library, only when actually constructed. Mirrors `extract_pymupdf.py`.

WhisperX needs the audio on disk, so the bytes are written to a temp file, transcribed,
word-aligned, and (if a HF token is configured) speaker-diarized, then mapped to the
engine's `Transcript`/`Word` shape.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.core.logging import get_logger
from app.ingestion.asr import Transcript, Word

log = get_logger(__name__)


class WhisperXAsr:
    """Transcribe + word-align (+ diarize) an mp3 using WhisperX."""

    def __init__(
        self,
        *,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        hf_token: str | None = None,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.hf_token = hf_token

    def transcribe(self, audio: bytes) -> Transcript:
        import whisperx  # lazy: only needed for the real path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.mp3"
            path.write_bytes(audio)

            model = whisperx.load_model(
                self.model_size, self.device, compute_type=self.compute_type
            )
            loaded = whisperx.load_audio(str(path))
            result = model.transcribe(loaded)

            align_model, metadata = whisperx.load_align_model(
                language_code=result["language"], device=self.device
            )
            aligned = whisperx.align(
                result["segments"], align_model, metadata, loaded, self.device
            )

            if self.hf_token:  # diarization folds a per-word "speaker" into `aligned`
                diarize = whisperx.DiarizationPipeline(
                    use_auth_token=self.hf_token, device=self.device
                )
                aligned = whisperx.assign_word_speakers(diarize(loaded), aligned)

            words: list[Word] = []
            for seg in aligned.get("segments", []):
                for w in seg.get("words", []):
                    if "start" not in w or "end" not in w:
                        continue
                    words.append(
                        Word(
                            text=w["word"],
                            start=float(w["start"]),
                            end=float(w["end"]),
                            speaker=w.get("speaker"),
                        )
                    )
        log.info("asr whisperx words=%d (model=%s)", len(words), self.model_size)
        return Transcript(words=tuple(words))
