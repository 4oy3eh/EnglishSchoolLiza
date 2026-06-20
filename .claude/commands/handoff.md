# Create Session Handoff Document

Create a handoff document before ending this session.

Analyze everything we've worked on and write `.claude/HANDOFF.md` with:

1. **Goal** — what we're building overall (English Test Platform; see docs/ARCHITECTURE.md).
2. **Current phase** — which phase of docs/DEVELOPMENT_PLAN.md we are on.
3. **Current State** — what's done, what's in progress, what's blocked.
4. **Next Steps** — exact numbered actions for the next session (usually the next Prompt in docs/PROMPTS.md).
5. **What Didn't Work** — approaches tried that failed (most important!).
6. **Files Modified** — list of changed files and what changed.
7. **Important Context** — anything non-obvious the next agent must know, including any invariant that was tricky.
8. **Verification** — the exact command to confirm the current state works (usually `make test`).

Keep it under 200 lines. Be specific — vague handoffs waste time.

After writing, confirm the file path and tell me to start the next session with:
`claude .claude/HANDOFF.md`