#!/usr/bin/env python3
"""Stub zcode CLI for engine tests. Invoked by the engine via a .cmd wrapper.

Mode comes from CS_STUB_MODE env (inherited through the engine's subprocess):
- "revise" : call 1 returns verdict revise (blocking + minor + fyi +
             repos_touched), later calls approve.      [happy loop]
- "openq"  : call 1 returns approve WITH an open question (engine must exit
             blocked-on-human), later calls approve.  [decisions resume]
- "approve": always approve.                          [implement paths]
- "badjson": call 1 returns unparseable prose (engine must re-ask, NOT burn
             an iteration), later calls approve.  [parse retry]

The per-plan call count persists in CS_STUB_STATE_DIR keyed by the --attach
plan path, so re-runs (e.g. after --decisions) see call 2.
Output: zcode --json shape {"response", "sessionId", "usage"} with the
verdict JSON as the response string.
"""
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

args = sys.argv[1:]
attach = None
for i, a in enumerate(args):
    if a == "--attach" and i + 1 < len(args):
        attach = args[i + 1]

mode = os.environ.get("CS_STUB_MODE", "revise")
state_dir = Path(os.environ.get("CS_STUB_STATE_DIR", tempfile.gettempdir())) / "cs-stub-state"
state_dir.mkdir(parents=True, exist_ok=True)
key = hashlib.md5((attach or "none").encode()).hexdigest()[:12]
counter = state_dir / f"{key}.count"
n = (int(counter.read_text()) if counter.exists() else 0) + 1
counter.write_text(str(n))

if mode == "badjson" and n == 1:
    # Unparseable reviewer output: prose, no JSON anywhere.
    print(json.dumps({
        "response": "Overall this plan looks reasonable to me. I read the "
                    "attached document and the code and have some thoughts, "
                    "but I will keep them informal. No JSON here.",
        "sessionId": f"stub-zcode-{n}",
        "usage": {"input_tokens": 1000, "output_tokens": 50},
    }))
    sys.exit(0)

if mode == "approve" or (n >= 2):
    verdict = {
        "verdict": "approve",
        "objections": [],
        "open_questions": [],
        "fyi_notes": ["stub fyi: everything looks fine"],
        "repos_touched": ["repoa"],
        "strengths": ["stub strength: rollback path is concrete"],
        "summary": "stub approval",
    }
elif mode == "openq":
    verdict = {
        "verdict": "approve",
        "objections": [],
        "open_questions": [{
            "question": "Ship the stub feature behind a flag?",
            "why": "controls rollout risk",
            "options": ["flag on", "flag off"],
            "recommendation": "flag on",
        }],
        "fyi_notes": [],
        "repos_touched": ["repoa"],
        "strengths": ["stub strength: rollback path is concrete"],
        "summary": "stub needs a human decision",
    }
else:
    verdict = {
        "verdict": "revise",
        "objections": [
            {"severity": "blocking", "point": "stub blocking objection 1"},
            {"severity": "minor", "point": "stub minor nit"},
        ],
        "open_questions": [],
        "fyi_notes": ["stub fyi note from iteration 1"],
        "repos_touched": ["repoa"],
        "strengths": ["stub strength: test strategy covers the crash"],
        "summary": "stub wants a revision",
    }

print(json.dumps({
    "response": json.dumps(verdict),
    "sessionId": f"stub-zcode-{n}",
    "usage": {"input_tokens": 1000, "output_tokens": 50},
}))
