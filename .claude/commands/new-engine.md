Scaffold a new bounded engine under app/<name>/ following the project's engine pattern.

Steps:
1. Read the root CLAUDE.md (invariants, layout) and docs/ARCHITECTURE.md (engine table).
2. Create app/<name>/ with: __init__.py, a service module, and an engine-local CLAUDE.md
   stating its inputs, outputs, and what it must NOT do (copy the row from the
   ARCHITECTURE engine table and expand it).
3. Wire the shared logger (`log = get_logger(__name__)`); no bare print().
4. Add tests/<name>/ with at least one passing placeholder test.
5. Do NOT cross engine boundaries — any shared type goes through contracts/.

Confirm the engine name and its single responsibility before creating files.
