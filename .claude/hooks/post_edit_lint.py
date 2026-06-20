#!/usr/bin/env python3
"""PostToolUse hook: runs ruff on edited .py files, nudges engine CLAUDE.md + CHANGELOG."""

import json
import subprocess
import sys
from pathlib import Path

# Force UTF-8 output so symbols don't crash on Windows cp1251 console.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def main() -> None:
    hook_input = json.loads(sys.stdin.read())

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    if tool_name not in ("Edit", "Write", "MultiEdit"):
        sys.exit(0)

    file_path = tool_input.get("file_path", "")
    if not file_path or not file_path.endswith(".py"):
        sys.exit(0)

    abs_path = Path(file_path).resolve()
    messages = []

    # 1. ruff check on the edited file
    try:
        result = subprocess.run(
            ["ruff", "check", str(abs_path), "--output-format=text"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        if result.returncode != 0 and result.stdout:
            messages.append(f"[ruff] Issues in {abs_path.name}:\n{result.stdout.strip()}")
        else:
            messages.append(f"[ruff] {abs_path.name} OK")
    except FileNotFoundError:
        messages.append("[ruff] not found — pip install ruff")
    except subprocess.TimeoutExpired:
        messages.append("[ruff] timed out")

    # 2. Engine CLAUDE.md reminder (kit uses per-engine CLAUDE.md, not README)
    parts = abs_path.parts
    if "app" in parts:
        i = parts.index("app")
        if i + 1 < len(parts):
            engine = parts[i + 1]
            engine_dir = Path(*parts[: i + 2])
            claude_md = engine_dir / "CLAUDE.md"
            if claude_md.exists():
                messages.append(f"[hook] Update app/{engine}/CLAUDE.md if the public contract changed")
            else:
                messages.append(f"[hook] No app/{engine}/CLAUDE.md — add one if this engine now has a contract")

    # 3. Invariant nudge: contracts changes need a migration + JSON Schema regen
    if "contracts" in parts:
        messages.append("[hook] contracts/ changed -> regenerate JSON Schema (make schema) AND add an Alembic migration")

    # 4. CHANGELOG reminder
    messages.append(f"[hook] Add a CHANGELOG.md entry for {abs_path.name} before /commit-phase")

    print("\n".join(messages))
    sys.exit(0)


if __name__ == "__main__":
    main()