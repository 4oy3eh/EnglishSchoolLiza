Ingest a Cambridge-style test into the item bank as a DRAFT for human review.

Inputs (ask if missing): question-paper PDF path, answer-key PDF path, optional audio dir.

Steps:
1. Read app/ingestion/CLAUDE.md and contracts/ first.
2. Run `make ingest path=<qp> key=<key> audio=<dir>` and watch the cmd logs for each
   pipeline step (extract -> images -> LLM draft -> key merge -> ASR/align -> validate).
3. If validation fails or any item is low-confidence, report exactly which items and why.
   Do NOT attempt to publish.
4. Summarize: how many sections/items per type were produced, which `correct` values came
   from the answer key, and which items are flagged for review.

Invariant: never auto-publish; the answer key is authoritative for `correct`; a human
approves in the admin review queue. (CLAUDE.md #5)
