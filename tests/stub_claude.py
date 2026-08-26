#!/usr/bin/env python3
"""Stub claude CLI for engine tests. Invoked by the engine via a .cmd wrapper.

Behavior is driven by the prompt it receives on stdin:
- "Implement the plan"  -> writes stub-implement.txt into CWD (proves the
  implement pass runs scoped to the target repo, junction-mapped), replies
  with short commentary.
- "REVIEWER OBJECTIONS" -> revises: extracts the CURRENT PLAN section and
  appends a revision section (proves the revise loop writes back).
- anything else          -> replies "OK".
Output: claude -p --output-format json shape {"result", "session_id", "usage"}.
"""
import json
import re
import sys
from pathlib import Path

prompt = sys.stdin.read()
if "Implement the plan" in prompt:
    (Path.cwd() / "stub-implement.txt").write_text("implemented by stub\n", encoding="utf-8")
    result = "stub implement pass done"
elif "REVIEWER OBJECTIONS" in prompt:
    m = re.search(r"=== CURRENT PLAN ===\n(.*?)\n=== END PLAN ===", prompt, re.DOTALL)
    base = m.group(1).strip() if m else "STUB PLAN"
    result = base + "\n\n## Stub revision\n\nAll reviewer objections addressed.\n"
else:
    result = "OK"
print(json.dumps({
    "result": result,
    "session_id": "stub-claude-1",
    "usage": {"input_tokens": 500, "output_tokens": 100},
}))
