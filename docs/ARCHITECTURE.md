# Architecture

## Engines (responsibilities & boundaries)

| Engine | Owns | Must NOT |
|---|---|---|
| `contracts` | All Pydantic schemas + generated JSON Schema. Source of truth. | Hold logic or DB code. |
| `content` | Item bank (tests/sections/items/assets), pooling, assignment, roster. | Decide if an answer is correct. |
| `ingestion` | PDF -> draft items, image crops, ASR, answer-key merge, review queue. | Auto-publish. Be the source of `correct` (answer key is). |
| `delivery` | Attempt lifecycle: window, server timer, serve-one-at-a-time, save answer, submit. | Send `correct` to client. Pause the timer. |
| `grading` | Deterministic grading + writing (LLM) + accepted-variants. | Read or change integrity data. |
| `telemetry` | Append-only event ingest (capture only). | Judge or score. |
| `integrity` | Deterministic features from the event stream (reproducible). | Call an LLM. Auto-fail. |
| `analysis` | LLM verdict over the integrity profile (advisory). | Be authoritative / auto-fail. |
| `admin` | Teacher API: roster, bank, review queue, results + verdict + replay. | Bypass human approval. |

## Data flow
```
Authoring/Ingestion ──► content (item bank, draft->published)
                                  │
student opens link ──► delivery (validate window + roster pick, resume on refresh)
                          │  serves items one-at-a-time, NO correct answers
        browser events ──►│ telemetry (append-only)
                          │
        on submit:        ├─► grading      -> score (deterministic + writing LLM)
                          ├─► integrity     -> deterministic feature profile
                          └─► analysis(LLM) -> suspicion verdict (advisory)
                                  │
teacher ◄── admin (score + verdict + raw replay, ranked suspicious-first)
```

## Item types (Cambridge A2 Key / B1 Preliminary)
Structure is **Section (shared stimulus) -> Items**, not flat questions.

- `single_choice` — N options (3 typical, 4 for PET Reading 3/5). Options are **text or
  image** (picture-choice listening, signs/notices). Option order is shuffleable.
- `gap_fill` / `short_text` — one word/number/date/time. Fields: `accepted[]` +
  `accepted_variants[]` (Cambridge "acceptable misspellings"). Grade = normalize ∈ accepted
  (+ rapidfuzz tolerance).
- `matching` — section-level option pool (A–H); each item maps to one pool option.
  Covers PET Reading Part 4 (sentences into gaps, with extra distractors) and KET
  Listening Part 5.
- `open_writing` — email/article/story with `word_min` and bullet points to cover.
  Fields: `rubric`, `grade_mode = llm | manual`.

Section `stimulus` is one of: `passage_text`, `audio_asset`, `image_set`, `gapped_text`,
`matching_pool`. Listening section settings: `{ plays: 2, look_through_seconds }`
(online needs no transfer phase — answers are already captured).

## Pooling / anti-leak
Different students get different **sections/passages drawn from a bank**, plus shuffled
options. Item order inside a section is fixed (follows the text/recording). Per-attempt
option permutation is stored server-side; the displayed index maps back to canonical
before grading.

## Access model
- One shareable link per test.
- Teacher uploads a **roster** (names). Student **picks their name** (no free typing).
- One attempt per roster entry. After submit, the name is locked (no retake).
- Refresh resumes the same attempt (token in URL/localStorage), never starts a new one.
- Admin sees a live roster: name -> not started / in progress / submitted + score +
  start/submit times.

## Audio ingestion (per listening section)
```
mp3 -> asr(WhisperX) -> { transcript, word_timestamps, segments, speakers }
   + QP(text+images) + answer_key  ->  LLM(Instructor) -> draft items, each with audio_span
   correct  <- answer_key (authoritative, NOT the transcript)
   -> review queue -> human approve -> published
```
Transcript is a comprehension/alignment aid (ASR mis-hears the answer-bearing tokens —
names, prices, spelled words). Authoritative `correct` always comes from the answer key.
