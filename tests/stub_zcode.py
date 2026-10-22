#!/usr/bin/env python3
"""Stub zcode CLI for engine tests. Invoked by the engine via a .cmd wrapper.

Mode comes from CS_STUB_MODE env (inherited through the engine's subprocess):
- "revise" : call 1 returns verdict revise (blocking + minor + fyi +
             repos_touched), later calls approve.      [happy loop]
- "openq"  : call 1 returns approve WITH an open question (engine must exit
             blocked-on-human), later calls approve.  [decisions resume]
- "openq2" : calls 1 AND 2 return TWO open questions (simulates the reviewer
             re-asking after answers were lost), call 3 demands a revise so
             the settle->revise path bakes decisions into the plan, later
             calls approve.            [cumulative decisions across rounds]
- "approve": always approve.                          [implement paths]
- "badjson": call 1 returns unparseable prose (engine must re-ask, NOT burn
             an iteration), later calls approve.  [parse retry]
- "approveminors": call 1 returns approve WITH a minor objection carrying a
             suggestion (engine must NOT end the loop - one more revise
             round), later calls approve.          [minors gate consensus]

Delegate-runner modes (the runner's prompt carries the COUNTERSIGN DELEGATE
preamble, so these branch on prompt content, not just CS_STUB_MODE):
- "delegate"         : edit a tracked file + create a brand-new UNADDED file
                       in the run worktree; JSON reply carries usage.
- "delegate_nousage" : same edits; JSON reply carries NO usage key.
- "delegate_limit"   : call 1 prints the ZCode usage-limit wording and exits
                       1 (mid-run limit); later calls (resume) do the edits.
- "delegate_commit"  : does the edits AND commits via the ABSOLUTE git path
                       (shim bypass: glm_committed becomes true).
- "delegate_hang"    : sleeps (SIGTERM-restore test).
- "delegate_fail"    : exits 1 with a generic error (config restore on crash).
For the engine's glm-implementer path the SAME "approve" mode serves both
roles: consensus reviews approve, delegate-prompt calls do the edits (a
"VERIFY OBJECTIONS" prompt applies the fix marker instead).

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
import time
from pathlib import Path

args = sys.argv[1:]
attach = None
resume = None
prompt_text = None
for i, a in enumerate(args):
    if a == "--attach" and i + 1 < len(args):
        attach = args[i + 1]
    if a == "--resume" and i + 1 < len(args):
        resume = args[i + 1]
    if a == "--prompt" and i + 1 < len(args):
        prompt_text = args[i + 1]

mode = os.environ.get("CS_STUB_MODE", "revise")
state_dir = Path(os.environ.get("CS_STUB_STATE_DIR", tempfile.gettempdir())) / "cs-stub-state"
state_dir.mkdir(parents=True, exist_ok=True)
# CS_STUB_KEY overrides the per-attach key: verify rounds attach round-varying
# patch files, so verify scenarios pin the count to one stable key.
key_source = os.environ.get("CS_STUB_KEY") or attach or "none"
key = hashlib.md5(key_source.encode()).hexdigest()[:12]
counter = state_dir / f"{key}.count"
n = (int(counter.read_text()) if counter.exists() else 0) + 1
counter.write_text(str(n))
if resume:
    # recorded so the e2e suite can assert the reviewer actually chains
    with open(state_dir / f"{key}.resumes", "a", encoding="utf-8") as f:
        f.write(resume + "\n")
if prompt_text is not None and os.environ.get("CS_STUB_RECORD_PROMPT"):
    (state_dir / f"{key}.lastprompt").write_text(prompt_text, encoding="utf-8")


def _write_rollout(session_id: str) -> None:
    """The real CLI records every model call under ~/.zcode/cli/rollout;
    mirror that so the runner's model_served lookup works in tests."""
    try:
        roll = Path.home() / ".zcode" / "cli" / "rollout"
        roll.mkdir(parents=True, exist_ok=True)
        eff = None
        try:
            pc = json.loads((Path.home() / ".zcode" / "v2" /
                             "provider_config.json").read_text(encoding="utf-8"))
            sel = (pc.get("config") or {}).get("defaultModelSelection") or {}
            eff = ((sel.get("options") or {}).get("reasoningLevel"))
        except (OSError, ValueError):
            pass
        rec = {"sessionId": session_id, "model": {"modelId": "GLM-5.3"},
               "request": {"body": {"output_config": {"effort": eff}}}}
        with open(roll / "model-io-stub.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _delegate_edits(root: Path, fix: bool) -> None:
    # cwd is the run worktree (single repo) or the workspace of links to the
    # worktrees (multi repo): pick the first directory holding README.md.
    roots = [root]
    try:
        roots += sorted(p for p in root.iterdir()
                        if p.is_dir() and not p.name.startswith("."))
    except OSError:
        pass
    target = next((r for r in roots if (r / "README.md").is_file()), root)
    if fix:
        (target / "delegate-fix-applied.txt").write_text(
            "fixed after reviewer objections\n", encoding="utf-8")
    else:
        readme = target / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "delegated line\n",
                          encoding="utf-8")
        (target / "glm-new-file.txt").write_text(
            "created by delegate stub\n", encoding="utf-8")   # never git-added
        (target / "glm-ignored.txt").write_text(
            "gitignored output\n", encoding="utf-8")          # excluded by diff


def _observe_config() -> None:
    """Record the zcode config state mid-run (assert effort/model were set)."""
    obs = os.environ.get("CS_STUB_OBSERVE")
    if not obs:
        return
    home = Path.home()
    entry = {"n": n}
    try:
        cli = json.loads((home / ".zcode" / "cli" / "config.json")
                         .read_text(encoding="utf-8"))
        entry["cli_model_main"] = (cli.get("model") or {}).get("main")
    except (OSError, ValueError):
        entry["cli_model_main"] = None
    try:
        pc = json.loads((home / ".zcode" / "v2" / "provider_config.json")
                        .read_text(encoding="utf-8"))
        entry["default_selection"] = ((pc.get("config") or {})
                                      .get("defaultModelSelection"))
    except (OSError, ValueError):
        entry["default_selection"] = None
    with open(obs, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


if prompt_text and "COUNTERSIGN DELEGATE" in prompt_text:
    # ---- delegate-runner branch (implement / resume / objection fix) -------
    fix = ("VERIFY OBJECTIONS" in prompt_text
           or "REVIEWER OBJECTIONS (JSON)" in prompt_text)
    _observe_config()
    if mode == "delegate_hang":
        time.sleep(600)
    if mode == "delegate_fail":
        print("stub delegate exploded before doing anything", file=sys.stderr)
        sys.exit(1)
    if mode == "delegate_concurrent":
        # Another writer (desktop app / engine heal) changes the effort key
        # while the run is in flight: the restore must NOT overwrite it.
        pc_path = Path.home() / ".zcode" / "v2" / "provider_config.json"
        try:
            pc = json.loads(pc_path.read_text(encoding="utf-8"))
            (pc.setdefault("config", {})
             .update({"defaultModelSelection":
                      {"providerId": "account:other-plan",
                       "modelId": "GLM-5.3"}}))
            pc_path.write_text(json.dumps(pc, indent=2), encoding="utf-8")
        except (OSError, ValueError):
            pass
    if mode == "delegate_limit" and n == 1:
        # The real CLI surfaces the spent 5-hour window as the turn's final
        # message (exit 0, session id known) - the shape the plan's test uses.
        print(json.dumps({
            "response": "Usage limit reached for 5 hour window. "
                        "Please wait for the window to reset.",
            "sessionId": f"stub-zcode-{n}",
            "usage": {"input_tokens": 100, "output_tokens": 1},
        }))
        sys.exit(0)
    _delegate_edits(Path.cwd(), fix)
    if mode == "delegate_commit":
        # bypasses the PATH shim exactly like a model invoking an absolute
        # git path would; the runner must still catch it (glm_committed)
        real_git = os.environ.get("CS_REAL_GIT") or "/usr/bin/git"
        subprocess_run = __import__("subprocess").run
        subprocess_run([real_git, "add", "-A"], cwd=str(Path.cwd()),
                       capture_output=True)
        subprocess_run([real_git, "commit", "-q", "-m", "stub glm commit"],
                       cwd=str(Path.cwd()), capture_output=True)
    reply = ("stub delegate fix applied" if fix
             else "stub delegate run done: edited README.md, created "
                  "glm-new-file.txt (unadded)")
    _write_rollout(f"stub-zcode-{n}")
    out = {"response": reply, "sessionId": f"stub-zcode-{n}"}
    if mode != "delegate_nousage":
        out["usage"] = {"input_tokens": 12000, "output_tokens": 600,
                        "cache_read_input_tokens": 9000}
    print(json.dumps(out))
    sys.exit(0)

if mode == "hang":
    # Reviewer never answers: lets the e2e suite SIGTERM the engine mid-call
    # and assert the unwind (lock released, interrupted report on stdout).
    time.sleep(600)

# ---- verify-stage modes (severity-gated: blocking gates, minors do not) ----
if mode in ("verifyclean", "verifyonce", "verifyminors", "verifyblock",
            "verifyopenq"):
    if mode == "verifyblock" or (mode == "verifyonce" and n == 1):
        verdict = {
            "verdict": "revise",
            "objections": [{"severity": "blocking",
                            "point": "stub blocking: empty input crashes the "
                                     "handler at src/handler.py:42",
                            "suggestion": "stub suggestion: guard the empty "
                                          "case and add a test"}],
            "open_questions": [],
            "fyi_notes": [],
            "repos_touched": ["repoa"],
            "strengths": ["stub strength: the change matches the intent"],
            "summary": "stub verify wants a fix",
        }
    elif mode == "verifyminors" and n == 1:
        verdict = {
            "verdict": "approve",
            "objections": [{"severity": "minor",
                            "point": "stub verify minor: extract the retry "
                                     "loop into a helper",
                            "suggestion": "stub suggestion: helper function"}],
            "open_questions": [],
            "fyi_notes": [],
            "repos_touched": ["repoa"],
            "strengths": ["stub strength: implementation satisfies the intent"],
            "summary": "stub verify approves with a minor note",
        }
    elif mode == "verifyopenq" and n == 1:
        verdict = {
            "verdict": "approve",
            "objections": [],
            "open_questions": [{"question": "Ship the verified fix behind a flag?",
                                "why": "controls rollout risk",
                                "options": ["flag on", "flag off"],
                                "recommendation": "flag on"}],
            "fyi_notes": [],
            "repos_touched": ["repoa"],
            "strengths": ["stub strength: implementation satisfies the intent"],
            "summary": "stub verify needs a human decision",
        }
    else:
        verdict = {
            "verdict": "approve",
            "objections": [],
            "open_questions": [],
            "fyi_notes": ["stub verify fyi: everything looks fine"],
            "repos_touched": ["repoa"],
            "strengths": ["stub strength: implementation satisfies the intent"],
            "summary": "stub verify approval",
        }
    print(json.dumps({
        "response": json.dumps(verdict),
        "sessionId": f"stub-zcode-{n}",
        "usage": {"input_tokens": 1000, "output_tokens": 50},
    }))
    sys.exit(0)

if mode == "openq2" and n <= 2:
    # Same two questions on both calls: if earlier answers were lost between
    # runs, the engine treats them as fresh forever and never reaches a revise.
    verdict = {
        "verdict": "approve",
        "objections": [],
        "open_questions": [
            {"question": "Q1: ship the stub feature free or Pro-only?",
             "why": "pricing decision", "options": ["free", "pro-only"],
             "recommendation": "free"},
            {"question": "Q2: include dark mode at launch?",
             "why": "scope decision", "options": ["yes", "no"],
             "recommendation": "no"},
        ],
        "fyi_notes": [],
        "repos_touched": ["repoa"],
        "strengths": ["stub strength: rollback path is concrete"],
        "summary": "stub needs two human decisions",
    }
    print(json.dumps({
        "response": json.dumps(verdict),
        "sessionId": f"stub-zcode-{n}",
        "usage": {"input_tokens": 1000, "output_tokens": 50},
    }))
    sys.exit(0)

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

if mode == "approveminors" and n == 1:
    verdict = {
        "verdict": "approve",
        "objections": [{"severity": "minor",
                        "point": "stub approve-with-minors: error handling "
                                 "is underspecified",
                        "suggestion": "stub suggestion: spell out the retry "
                                      "policy"}],
        "open_questions": [],
        "fyi_notes": [],
        "repos_touched": ["repoa"],
        "strengths": ["stub strength: rollback path is concrete"],
        "summary": "stub approves but wants one more polish round",
    }
elif mode == "openq2" and n == 3:
    verdict = {
        "verdict": "revise",
        "objections": [{"severity": "blocking",
                        "point": "stub blocking: bake the settled human "
                                 "decisions into the plan"}],
        "open_questions": [],
        "fyi_notes": [],
        "repos_touched": ["repoa"],
        "strengths": [],
        "summary": "stub wants decisions incorporated",
    }
elif mode == "approve" or mode.startswith("delegate") or (n >= 2):
    verdict = {
        "verdict": "approve",
        "objections": [],
        "open_questions": [],
        "fyi_notes": ["stub fyi: everything looks fine"],
        "repos_touched": ["repoa"],
        "strengths": ["stub strength: rollback path is concrete"],
        "summary": "stub approval",
    }
    if mode == "badjson":
        # Pad the raw reply past the old 4000-char truncation so the e2e test
        # can prove the full text is persisted.
        verdict = dict(verdict, fyi_notes=["stub fyi padding: " + "x" * 4200])
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

_write_rollout(f"stub-zcode-{n}")
print(json.dumps({
    "response": json.dumps(verdict),
    "sessionId": f"stub-zcode-{n}",
    "usage": {"input_tokens": 1000, "output_tokens": 50},
}))
