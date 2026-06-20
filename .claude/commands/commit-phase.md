# Commit the current phase

Record the current phase in git. Do this only when the phase is actually done.

1. **Gate first.** Run the `verifier` subagent (or `make lint && make test`). If anything
   is red, STOP — report what failed and do not commit.
2. **Changelog.** Update `CHANGELOG.md` (Keep a Changelog style) with an entry for this
   phase: a short `Added` / `Changed` / `Fixed` summary of what it delivered.
3. **Commit** with a conventional message tied to the phase:
   ```
   git add -A
   git commit -m "<type>(phase-N): <one-line summary>"
   ```
   `<type>` = feat | fix | test | docs | chore. N = the phase number.
4. **Push** to the remote:
   ```
   git push origin <current-branch>
   ```
   (Repo: https://github.com/4oy3eh/EnglishSchoolLiza — drop this step if you want local-only.)
5. Report the commit hash and summary, then suggest running `/handoff` and `/clear`
   before the next phase.

Never commit with a red gate. Keep the changelog entry concise.