#!/usr/bin/env python3
"""Stub claude CLI for engine tests. Invoked by the engine via a .cmd wrapper.

Behavior is driven by the prompt it receives on stdin (plus CS_STUB_MODE /
CS_CLAUDE_MODE):
- "VERIFY FIX"          -> verify-stage fixer: touches a file in cwd and
  replies with the fix summary the next review round must see.
- "VERIFY stage"        -> verify-stage REVIEWER (claude as --verify-reviewer
  claude, or the glm-implementer path): replies a verdict JSON. Modes (from
  CS_CLAUDE_MODE, falling back to CS_STUB_MODE): "verifyonce" blocks on the
  first review call then approves (exercises the fix loop); anything else
  approves. The per-key call count persists in CS_STUB_STATE_DIR so re-runs
  see call 2.
- "Implement the plan"  -> writes stub-implement.txt into CWD (proves the
  implement pass runs scoped to the target repo, junction-mapped), replies
  with short commentary.
- "REVIEWER OBJECTIONS" -> revises: extracts the CURRENT PLAN section and
  appends a revision section (proves the revise loop writes back).
  With CS_STUB_MODE=truncate, the FIRST revise reply is only the first line
  of the plan (a truncated output turn, far under the engine's shrink gate);
  the re-ask (recognized by the TRUNCATED marker the engine appends) gets
  the full revision. CS_STUB_MODE=truncate2 truncates every reply.
  CS_STUB_MODE=spendlimit fails every call like the real CLI's monthly
  spend-limit error: JSON on stdout, exit 1, empty stderr.
- anything else          -> replies "OK".
Output: claude -p --output-format json shape {"result", "session_id", "usage"}.
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

prompt = sys.stdin.read()
mode = os.environ.get("CS_STUB_MODE", "")
claude_mode = os.environ.get("CS_CLAUDE_MODE", "") or mode
state_dir = Path(os.environ.get("CS_STUB_STATE_DIR",
                                os.path.join(os.environ.get("TMPDIR", "/tmp"),
                                             "cs-claude-state"))) / "cs-stub-state"
state_dir.mkdir(parents=True, exist_ok=True)
key_source = os.environ.get("CS_STUB_KEY") or "claude"
key = hashlib.md5(key_source.encode()).hexdigest()[:12]
counter = state_dir / f"{key}.count"
n = (int(counter.read_text()) if counter.exists() else 0) + 1
counter.write_text(str(n))

if mode == "spendlimit":
    # The real claude CLI in -p --output-format json mode writes its errors as
    # JSON on STDOUT and exits non-zero; stderr stays empty. Reproduce that
    # exact shape (monthly spend limit) for the engine's error-recovery test.
    print(json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": ("You've hit your monthly spend limit · raise it at "
                   "claude.ai/settings/usage. Your limit resets 1:20am."),
        "session_id": "stub-claude-err",
    }))
    sys.exit(1)

model = "claude-stub-4.5"


def verdict_reply(verdict):
    print(json.dumps({
        "result": json.dumps(verdict),
        "session_id": f"stub-claude-{n}",
        "model": model,
        "usage": {"input_tokens": 500, "output_tokens": 100},
    }))


if "VERIFY FIX" in prompt:
    # verify-stage fixer: touch a file in cwd (proves it ran against the
    # workspace) and reply with the fix summary the next review round must see
    marker = Path.cwd() / "verify-fix-applied.txt"
    count = int(marker.read_text().strip() or 0) + 1 if marker.exists() else 1
    marker.write_text(str(count), encoding="utf-8")
    result = (f"stub fix round {count}: guarded the empty-input case in "
              "src/handler.py and added a regression test")
elif "VERIFY stage" in prompt:
    # verify-stage REVIEWER (claude reviewing the diff): same verdict schema
    # the GLM reviewer gets, so parse_verdict is exercised unchanged.
    if claude_mode == "verifyonce" and n == 1 and "PRIOR VERIFY ROUNDS" not in prompt:
        verdict_reply({
            "verdict": "revise",
            "objections": [{"severity": "blocking",
                            "point": "stub claude blocking: the delegated "
                                     "handler never guards empty input "
                                     "(README.md:1)",
                            "suggestion": "stub suggestion: guard it and add "
                                          "a regression test"}],
            "open_questions": [],
            "fyi_notes": [],
            "repos_touched": ["repoa"],
            "strengths": ["stub strength: the diff matches the intent"],
            "summary": "stub claude reviewer wants a fix round",
        })
    else:
        verdict_reply({
            "verdict": "approve",
            "objections": [],
            "open_questions": [],
            "fyi_notes": ["stub verify fyi: everything looks fine"],
            "repos_touched": ["repoa"],
            "strengths": ["stub strength: implementation satisfies the intent"],
            "summary": "stub claude reviewer approval",
        })
    sys.exit(0)
elif "Implement the plan" in prompt:
    (Path.cwd() / "stub-implement.txt").write_text("implemented by stub\n", encoding="utf-8")
    result = "stub implement pass done"
elif "REVIEWER OBJECTIONS" in prompt:
    m = re.search(r"=== CURRENT PLAN ===\n(.*?)\n=== END PLAN ===", prompt, re.DOTALL)
    base = m.group(1).strip() if m else "STUB PLAN"
    o = re.search(r"=== REVIEWER OBJECTIONS \(JSON\) ===\n(.*?)\n=== END OBJECTIONS ===",
                  prompt, re.DOTALL)
    objections = o.group(1).strip() if o else "(none received)"
    result = (base + "\n\n## Stub revision\n\nAll reviewer objections addressed.\n\n"
              "### Objections received\n\n" + objections + "\n")
    truncated = "\n".join(base.splitlines()[:1]) or "(truncated)"
    if mode == "truncate2" or (mode == "truncate"
                               and "PREVIOUS reply was TRUNCATED" not in prompt):
        result = truncated
else:
    result = "OK"
print(json.dumps({
    "result": result,
    "session_id": "stub-claude-1",
    "model": model,
    "usage": {"input_tokens": 500, "output_tokens": 100},
}))
