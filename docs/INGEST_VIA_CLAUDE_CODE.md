# Runbook — Manual ingest via Claude Code (`pdf` skill, no paid API)

**Audience:** a Claude Code session (running on the Claude subscription) tasked with
turning a Cambridge sample test into a **human-approved, published** test in the item
bank — **without** the automated `make ingest` pipeline (which needs PyMuPDF + a paid
`ANTHROPIC_API_KEY`).

This is "Option B": *you* (Claude Code) are the extractor + structurer. You read the
PDFs with the `pdf` skill, build a `contracts.Test`, load it as a **draft**, the human
reviews it, then you publish. The answer key still comes only from the answer-key PDF —
you never invent it (golden rule #5).

> Automated path for later, when keys/deps exist: `.claude/commands/ingest-pdf.md`
> (`make ingest`). This runbook is the subscription-only alternative.

---

## 0. Read first (do not skip)
- `CLAUDE.md` — golden rules. The ones that bind this task: **#1** (no `correct` to the
  client — handled structurally by the `Client*` projection, so just don't leak keys
  into prompts/stimulus text), **#5** (draft → human approval → publish; answer key is
  authoritative), **#7** (never reorder items within a passage/recording), **#8** (log
  every LLM call — N/A here, there is no API call).
- `contracts/content.py` — the exact shapes you must produce (recapped in §4).
- `app/content/seed.py` — the **working template** for building a `Test` + assets and
  loading them. You will copy this pattern (see §6).

---

## 1. The data (local, gitignored — `assets/`)

### A2 Key for Schools 2022 — `assets/a2/` ✅ start here (internally consistent)
| File | Role |
| --- | --- |
| `A2_Key_for_Schools_2022_sample_Reading_and_Writing_questions_v2.pdf` | Reading & Writing **question paper** |
| `A2_Key_for_Schools_2022_sample_Reading_and_Writing_answer_key.pdf` | R&W **answer key** (authoritative `correct`) |
| `A2_Key_for_Schools_2022_sample_tests_Listening_question_paper.pdf` | Listening **question paper** |
| `A2 Key for Schools 2022 sample tests Listening - answer key.pdf` | Listening **answer key** |
| `a2-key-for-schools-listening-sample-test.mp3` | Listening **audio** (one file, all parts) |
| `a2-key-for-schools-listening-sample-test.transcript.txt` | Listening **transcript** (use this; skip WhisperX) |

### B1 Preliminary — `assets/b1/` ⚠️ year mismatch — verify before trusting keys
QPs are **2022** (`Reading - QP`, `Listening - QP`, `Writing - QP`) but the answer keys
are **2020** (`Reading - answers`, `Listening - AK`). The 2020 keys may not line up with
the 2022 questions by number. **Do not ingest B1 until you have confirmed, question by
question, that the key matches the QP.** If they don't match, stop and report — a wrong
key silently corrupts grading (golden rule #5). Prefer A2 for the first real ingest.

> Writing parts have **no** answer key (graded by rubric / `open_writing`), which is
> expected — that's not a missing-key error.

---

## 2. Where the bytes must land
Assets are served by `GET /assets/{asset_id}` from `settings.assets_dir`
(**default `./var/assets`**). `FilesystemStorage` stores each blob as a flat file named
exactly `<assets_dir>/<asset_id>`. So an `asset_id` you choose **is** the filename.

- Use clear, collision-free ids with a real extension so the content type is right:
  e.g. `a2-2022-listening.mp3`, `a2-2022-r1-q1-sign.png`.
- Either call `ContentService.add_asset(asset_id, data, content_type=...)` (preferred —
  it logs) or place the file directly at `var/assets/<asset_id>`. The runner then loads
  `<audio src="/assets/a2-2022-listening.mp3">` etc.
- The listening **mp3** must be stored as an asset and referenced by every listening
  section's `audio_asset.asset_id` (there is one mp3 for all parts — see pitfalls §8).

---

## 3. Output you are producing
A single `contracts.Test` (`status="draft"`) whose sections/items mirror the Cambridge
paper, plus the asset blobs it references. You load it via the seed pattern (§6), the
human reviews (§7), you publish + add a roster (§7).

---

## 4. Contract recap (produce EXACTLY these shapes)
`Level = "A2_KEY" | "B1_PRELIMINARY"` · `Skill = "reading" | "listening" | "writing"`
· `status = "draft"` (always, until human approval).

**Section**: `id`, `title?`, `skill?`, **one** `stimulus`, `items` (≥1).

**Stimulus (pick one per section):**
- `PassageTextStimulus{ text }` — reading passage / notice text.
- `AudioAssetStimulus{ asset_id, plays=2, look_through_seconds=0 }` — listening.
- `ImageSetStimulus{ asset_ids[≥1] }` — sign/picture stimulus.
- `GappedTextStimulus{ text }` — gapped passage (Reading open cloze).
- `MatchingPoolStimulus{ options: PoolOption{key,text}[≥2] }` — the shared A–H pool for
  `matching` items in this section.

**Item (answer-key family — `correct`/`accepted` live ONLY here, never on `Client*`):**
- `SingleChoiceItem{ id, prompt, options[≥2], correct }` where each option is
  `TextOption{key,text}` or `ImageOption{key, asset_id, alt?}`; `correct` = the winning
  option's `key`.
- `GapFillItem{ id, prompt, accepted[≥1], accepted_variants=[] }` — `accepted` = exact
  acceptable answers from the key; `accepted_variants` = Cambridge "acceptable
  misspellings" only (don't pad it).
- `MatchingItem{ id, prompt, correct }` — `correct` = a `key` from the section's
  `MatchingPoolStimulus`.
- `OpenWritingItem{ id, prompt, word_min(>0), bullet_points=[], rubric, grade_mode="llm" }`
  — `rubric` is authoring-only guidance; never served.

**Test**: `id`, `title`, `level`, `status="draft"`, `duration_minutes(>0)`, `sections[≥1]`.

---

## 5. Cambridge Part → contract (A2 Key — a GUIDE; the PDF is the source of truth)
Read the actual PDF and follow what's printed; this table is orientation, not gospel.

| Paper / Part | Typical shape | Map to |
| --- | --- | --- |
| Reading P1 | short signs/notices, 3 options each (own image per Q) | one **single-item section per sign**: `ImageSetStimulus`(sign) + `SingleChoiceItem` |
| Reading P2 | match people to texts | `MatchingPoolStimulus` + `MatchingItem`s |
| Reading P3 | long text, 3-option questions | `PassageTextStimulus` + `SingleChoiceItem`s |
| Reading P4 | gapped text, choose the word | `GappedTextStimulus` + `SingleChoiceItem`s |
| Reading P5 | open cloze / word formation | `PassageTextStimulus`(or gapped) + `GapFillItem`s |
| Writing P6 | short message (~25 words) | `OpenWritingItem{word_min, bullet_points, rubric}` |
| Writing P7 | picture-prompted story (~35 words) | `OpenWritingItem` |
| Listening P1 | 5×3-option with pictures | `AudioAssetStimulus` + `SingleChoiceItem`(`ImageOption`s) |
| Listening P2 | note/gap completion | `AudioAssetStimulus` + `GapFillItem`s |
| Listening P3/4 | 3-option multiple choice | `AudioAssetStimulus` + `SingleChoiceItem`s |
| Listening P5 | matching | `MatchingPoolStimulus` (no audio stimulus on this section — see §8) |

Keep **item order within a section exactly as printed** (rule #7).

---

## 6. Procedure
1. **Extract the QP** with the `pdf` skill → text + per-page layout. Identify each Part,
   each question, each prompt, and each option exactly as printed.
2. **Extract images** the questions depend on (Reading P1 signs, Listening P1 picture
   options). Save each as an asset (§2): pick an `asset_id` ending in `.png`/`.jpg`,
   write the bytes to `var/assets/<asset_id>` (or via `add_asset`). Reference them as
   `ImageOption.asset_id` / `ImageSetStimulus.asset_ids`.
3. **Store the mp3** as an asset; reference it from each listening section's
   `audio_asset.asset_id`. Use the bundled `*.transcript.txt` if you need the text —
   **do not** run ASR.
4. **Parse the answer-key PDF** → a `{question_number: answer}` map. Set `correct`
   (single_choice/matching) and `accepted` (gap_fill) **only** from this. If a question
   has no key entry, or a `correct` isn't among the item's option keys → **stop and
   report that item**; do not guess (rule #5).
5. **Build the `Test`** (`status="draft"`) as a Python builder modeled on
   `app/content/seed.py` (`build_*_test()` returning a `contracts.Test`, plus a
   `sample_assets()`-style dict of `asset_id -> (bytes, content_type)`). Stable,
   descriptive ids (e.g. `a2-2022`, `a2-2022-sec-r1-q1`, `a2-2022-r1-q1`).
6. **Validate**: constructing the `Test` Pydantic model must not raise; every
   `ImageOption.asset_id` / `audio_asset.asset_id` / `ImageSetStimulus.asset_ids` you
   reference must exist in your assets dict; every keyed item must have a `correct`/
   `accepted` from the key.
7. **Load as draft**: run your builder against the dev DB exactly like
   `app/content/seed.py` does — write assets through `ContentService.add_asset`, then
   `create_test(test)`. **Do NOT call `publish()` yet.**

> Loader (BUILT — see §10): use the generic `app/content/load_test.py`. A per-test
> Python builder calls `load_test(session, storage, test, assets)` (draft by default;
> `publish=`/`roster=` are opt-in post-review). For the A2 ingest that builder is
> `app/content/ingest_a2_2022.py` (`make ingest-a2`). Tests authored as JSON can use the
> CLI instead: `python -m app.content.load_test --file <test.json> [--assets-dir <dir>]`.

---

## 7. Human review → publish (golden rule #5)
Hand the human a short report and let them approve **before** publishing:
- counts per section and per item type;
- every `correct`/`accepted` value with the question number it came from in the key PDF;
- any item you flagged (no key entry, ambiguous option, structural decision from §8);
- a note that they can open the draft in the teacher review queue / dashboard.

After explicit approval: flip `draft → published` (`ContentService.publish(test_id)` or
the admin review-queue endpoint), then add a roster
(`RosterService.add_student` / `AttemptRepository.add_roster_entry`) so there are names
to pick on the share link. Verify in a browser: assets load via `/assets/{id}`, audio
plays, the runner serves keyless items.

---

## 8. Pitfalls / decisions to flag for the reviewer
- **One stimulus per section.** A `matching` listening part needs a `MatchingPoolStimulus`,
  which can't also be an `audio_asset`. Model such a part without the audio on the
  section (put audio context in the title/prompt) and flag the trade-off — or split.
- **One mp3 for all listening parts.** The contract has no audio start/end offset
  (`audio_span` is ingestion-internal, never persisted). Referencing the whole file
  means a student replays the entire listening test per section. Acceptable for a demo;
  flag it. (Splitting the mp3 into per-part clips and storing each as its own asset is
  the cleaner fix if the human wants it.)
- **Per-question stimulus (Reading P1).** When each question has its own sign image,
  make it its own single-item section so the image binds to that question (rule #7 still
  holds — you're not reordering within a section).
- **Never leak the key.** Don't put the answer into a prompt, option `alt`, or stimulus
  text. `correct`/`accepted` live only on the authoring item fields.
- **B1 year mismatch** (§1): confirm key↔QP alignment before trusting it.
- **Assets dir.** Put blobs where the route serves from (`./var/assets`), not in
  `./assets/` (the raw Cambridge originals).

---

## 9. Acceptance checklist (done = all true)
- [ ] `Test` builds against `contracts` with no validation error; `status` was `draft`
      until human approval.
- [ ] Every referenced `asset_id` exists in `var/assets` and loads via `/assets/{id}`.
- [ ] Every keyed item's `correct`/`accepted` traces to the answer-key PDF by question
      number; nothing invented.
- [ ] Item order within each section matches the printed paper (rule #7).
- [ ] Human approved; only then `published`; roster added; verified in the browser.

---

## 11. YLE Movers (`test_id="movers-vol2"`) — all-in-one PDF, ASR, pictures, interactivity
Done after A2/B1; the new wrinkles, so the next young-learner ingest is faster.
**Now made child-friendly** (pictures everywhere + the interactive parts): 11
sections, 62 items, 41 assets. Engine additions this needed (all general-purpose):

- **Contract: context images on a stimulus.** Added `images: list[str]` to
  `passage_text` / `gapped_text` / `audio_asset` (JSON field on the stimulus — no
  migration). YLE constantly needs a picture beside a passage / note form / the audio
  without breaking "one stimulus per section". The runner renders them
  (`contextImagesHTML` in `exam.js`); `load_test.referenced_asset_ids` validates them.
- **Contract: `ColourTaskItem` (+ `ClientColourTaskItem`).** A 'listen and colour'
  task: a line-art `asset_id` (transparent-background PNG) + a fixed `palette`; the
  colouring `key` is authoring-only (dropped by projection, like `rubric`). It is
  **teacher-reviewed** — grading routes `colour_task` → `needs_review` (method
  `colour_manual`), never auto-scored. New `GradeMethod` value `colour_manual`.
- **Runner: a colouring canvas** (`exam.js` `wireColour`). The transparent line-art
  sits over a paint `<canvas>`; one medium brush (radius = `canvas.width*0.045`, so
  it's relative to the *image*, consistent on every device) paints the white areas;
  palette = blue/green/red/brown + a rubber (`destination-out`). On each stroke it
  saves the flattened picture (data-URL) as the answer for the teacher; telemetry gets
  a compact `[image Nb]` marker, not the blob. The colour PNG → white background made
  transparent with PIL (`alpha=0` where `rgb>225`).
- **Draw-lines / match-pictures → auto-graded picture-choice.** Listening Part 1
  ("draw lines names↔people") became `single_choice` "Which child is X?" with the 8
  child cut-outs as `ImageOption`s; Part 3 ("match days↔pictures") became
  `single_choice` "What did Sally do on <day>?" with the 6 activity pictures. The
  day→picture and child→name mappings were taken from the transcript + Marking Key.
  Cropping 8 overlapping children needs per-figure boxes (autotrim won't separate
  them); they're recognisable thumbnails even with a little neighbour bleed.
- **Still omitted:** Speaking (face-to-face). Everything else is in.

### Earlier (first Movers pass) — the still-true basics:

- **All-in-one PDF.** One file held Listening + Reading & Writing + Speaking **and**
  the Marking Keys (and the tapescript). Grep for `Marking Key` / `Part N marks` to
  find the keys; they are authoritative as usual. The inline tapescript is two-column
  and scrambles under `-layout` — prefer the ASR transcript for clean text.
- **No transcript → transcribe yourself.** `faster-whisper` (CTranslate2, **no torch**)
  is installed: `WhisperModel("small", device="cpu", compute_type="int8")`,
  `vad_filter=True`. ~28 min audio transcribed in a couple of minutes. Save it next to
  the mp3 as `*.transcript.txt` (same header/`[mm:ss–mm:ss]` format as the bundled
  ones) — it's the deliverable + gives timestamps for the preview split (here `00:45`,
  "Look at part one") and corroborates the keys.
- **New level needs a contract change.** `Level` had only A2/B1; added `"A1_MOVERS"`
  (`make schema`, no migration — `level` is a plain column). Add the enum value before
  building or the `Test` won't validate.
- **Item-type mapping.** Listening 2 (`gap_fill`) & 4 (`single_choice` images); R&W
  1–6 (`gap_fill` for word/sentence completion, `single_choice` for yes/no + dialogue
  + cloze, one title `single_choice`). Parts 1/3/5 were *first* skipped as
  "not gradeable", then brought in via the picture-choice / colour-task additions
  above — only Speaking stays out. (The first pass was 8 sections / 50 items; the
  child-friendly version is 11 / 62.)
- **Marking-key notation → `accepted`.** `( )` = optional extra words, `/` = alt word,
  `//` = alt complete answer. Expand into `accepted`, e.g. `(the) parrot(s)` →
  `["parrot","parrots","the parrot","the parrots"]`; `mum//mother//mummy` →
  `["mum","mother","mummy"]`. For R&W Part 1 the key gives the word *with* its article
  (`a whale`); I accepted the bare noun too (kids copy or not) — a flagged judgment.
- **Key typo caught (rule #5).** R&W Part 5 Q10 key prints `the/Peter's family` but no
  "Peter" exists in that story → accepted only `the family`, flagged for the reviewer.

## 10. Lessons from the first real ingest (A2 Key 2022 — `test_id="a2-2022"`)
What actually happened doing this for real, so the next ingest is faster.

### Tooling (this box: no PyMuPDF, no `pdfimages`/`pdftoppm`)
- **Text:** `pdftotext -layout <pdf> <out.txt>` (poppler, already on PATH via mingw).
  Good enough to transcribe every QP/answer-key. Write outputs under `var/extract/`
  (gitignored) and read them with the Read tool — `/tmp` is not Read-tool-visible.
- **Images:** there is **no** raster extractor here, and the signs/pictures are mostly
  vector anyway. Install `pypdfium2` + `Pillow` (OSS, self-contained renderer, *not*
  PyMuPDF, no API key) and **render the page, then crop**. Auto-trim each crop with
  `PIL.ImageChops.difference` against white to get tight bounding boxes. Detect picture
  rows by summing dark pixels per row rather than eyeballing pixel offsets — manual
  bands were off by a whole row on one page.
- **Verify crops with the Read tool** (it shows images) before saving them as assets.

### Answer-key extraction gotcha (cost real time)
- `pdftotext -layout` **mis-aligned the A2 Listening Part 2 gap answers**: the surname
  answer floated up onto the `Part 2` header line and Q10 looked blank. The true
  reading order came from **`pdftotext` *without* `-layout`** (`... | -`) and was
  cross-checked against the `*.transcript.txt`. Result: `6=Fairford, 7=Friday,
  8=7.30, 9=train, 10=boots`. **Always sanity-check note-completion keys against the
  transcript** — one shifted column silently corrupts grading (rule #5).

### Part → contract decisions actually taken (deviations from the §5 table)
- **Reading Part 2 (match people to texts) → `PassageTextStimulus` + `single_choice`**,
  *not* `MatchingPoolStimulus`. The three people (Amy/Flora/Louisa) are the A/B/C
  options, but the three readable paragraphs have nowhere to live in a matching pool
  (one stimulus per section). Putting all three paragraphs in a passage + repeating the
  A=Amy/B=Flora/C=Louisa options per question is faithful and gradeable. **Verified the
  paragraph→person assignment against all 7 keys** before trusting it.
- **Reading Part 4 (gapped word choice) → `GappedTextStimulus` + `single_choice`**
  (3 word options per gap). **Part 5 (open cloze) → `GappedTextStimulus` + `gap_fill`.**
  Note Q25's key lists **two** accepted words (`your / the`) → `accepted=["your","the"]`.
- **Writing Part 7 → `ImageSetStimulus`(3 story pics) + `open_writing`.** Part 6 → a
  `PassageTextStimulus` scenario + `open_writing` (bullets from the paper).
- **Listening Part 1 → ONE section** (`audio_asset` + 5 `single_choice` with
  `ImageOption`s), not five per-question sections — unlike Reading Part 1, the audio is
  shared across the five, so the per-question-stimulus split (§8) does not apply.
- **`gap_fill` accepted values are taken verbatim from the key** — `grading.normalize`
  casefolds + collapses whitespace, so case/spacing never needs variants. Internal
  punctuation is *not* folded: a time like `7.30` won't match `7:30` (flagged Q8).

### Structural facts confirmed (§8 pitfalls, now real)
- **One Test combines R&W + Listening** (17 sections, 57 items), `duration_minutes=95`
  (R&W 60' + Listening ~35'); the real exam administers them separately — flag for the
  reviewer.
- **Listening Part 5 (matching) carries NO `audio_asset`** — its stimulus must be the
  `MatchingPoolStimulus`. The block's shared player (anchored on Part 1) keeps playing.
- **Audio is split into a free intro preview + a single-play test track.** The source
  mp3 was cut (lossless `ffmpeg -c copy`, via the pip-bundled `imageio-ffmpeg`) at the
  intro boundary (`00:32`, where "Now look at the instructions for part one" begins):
  - `a2-2022-listening-preview.mp3` — the opening explanation, freely replayable;
  - `a2-2022-listening.mp3` — the test itself (Cambridge's baked-in double play kept).
  `AudioAssetStimulus` gained `preview_asset_id` + `locked` (contract change → `make
  schema`; no migration — the stimulus is a JSON column). Part 1's section anchors the
  block: it sets `preview_asset_id` and `plays=1`; Parts 2–4 are `plays=1`, no preview.
  `load_test.referenced_asset_ids` now also validates `preview_asset_id`.

### Loader (Step 2 outcome)
Built **two** complementary pieces (justification: a 24-image, 22 MB-audio test is far
cleaner as a typed Python builder than a hand-authored JSON, but the *mechanism* should
be reusable):
- `app/content/load_test.py` — generic, tested loader. `load_test(session, storage,
  test, assets, *, roster=(), publish=False)` validates that every referenced
  `asset_id` is present (raises otherwise), stores blobs, creates the test **as a
  draft**, and is idempotent (replaces on re-run). Also a JSON CLI
  (`--file/--assets-dir/--roster/--publish`) for future JSON-authored tests.
- `app/content/ingest_a2_2022.py` — the A2 per-test builder (the authored content +
  provenance), runs via `make ingest-a2`. Loads as a draft; no publish, no roster.
- Tests: `tests/test_load_test.py` (draft-by-default, asset storage, missing-asset
  guard, opt-in publish/roster, idempotency).
