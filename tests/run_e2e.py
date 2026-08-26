#!/usr/bin/env python3
"""End-to-end engine test with stub agents - zero model spend.

Scenarios (sequential, each with its own plan file and fake repos):
  A. happy loop        : revise -> revise -> approve => consensus, exit 0
  B. open questions    : approve+question => exit 4; --decisions rerun => consensus
  C. branch block      : approve + --implement, repo on main => exit 5, no edits
  D. implement         : same but repo on feature branch => exit 0, file lands
                         in the REAL repo through the junction workspace
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / "scripts" / "countersign_loop.py"
PY = sys.executable

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run_engine(plan, repos, extra=(), stub_mode="revise"):
    env = dict(os.environ,
               CS_STUB_MODE=stub_mode,
               CS_STUB_STATE_DIR=str(STATE_DIR),
               CS_DISABLE_POWERSHELL_JUNCTION="1")  # unused; placeholder symmetry
    cmd = [PY, str(ENGINE), str(plan),
           "--claude-cli", str(STUB_CLAUDE), "--zcode-cli", str(STUB_ZCODE),
           "--heartbeat", "0", "--max-retries", "0", *extra]
    for r in repos:
        cmd += ["--link-repo", str(r)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=300)
    report = None
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            report = json.loads(line)
            break
        except ValueError:
            continue
    return proc.returncode, report, proc.stderr


def make_repo(path, branch):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True,
                   capture_output=True)
    (path / "README.md").write_text("stub repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=s@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "init"], cwd=path, check=True,
                   capture_output=True)


def make_plan(repo, name, text):
    p = repo / "docs" / "plans" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


with tempfile.TemporaryDirectory(prefix="cs-e2e-") as td:
    T = Path(td)
    STATE_DIR = T / "stub-state"

    # .py stubs are invoked directly via sys.executable (the engine supports
    # .py paths for both CLIs) - no cmd.exe wrapper quoting in the loop.
    STUB_CLAUDE = HERE / "stub_claude.py"
    STUB_ZCODE = HERE / "stub_zcode.py"

    repoA = T / "repoa"          # starts on main (branch-block scenario), switched later
    repoB = T / "repob"
    make_repo(repoA, "main")
    make_repo(repoB, "dev")

    print("scenario A: happy consensus loop")
    planA = make_plan(repoA, "plan-a.md", "# Plan A\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planA, [repoA, repoB], stub_mode="revise")
    check("exit code 0", rc == 0, f"rc={rc}")
    check("outcome consensus", rep and rep.get("outcome") == "consensus", str(rep)[:120])
    check("iterations_used == 2", rep and rep.get("iterations_used") == 2)
    check("plan rewritten in place", "Stub revision" in planA.read_text(encoding="utf-8"))
    hist = Path(rep["history_dir"])
    check("snapshot plan-v01.md", (hist / "plan-v01.md").is_file())
    check("review-iter-01.json", (hist / "review-iter-01.json").is_file())
    check("fyi surfaced (cumulative)",
          rep.get("fyi_notes") == ["stub fyi note from iteration 1",
                                   "stub fyi: everything looks fine"])
    check("strengths cumulative in report",
          rep.get("strengths") == ["stub strength: test strategy covers the crash",
                                   "stub strength: rollback path is concrete"],
          str(rep.get("strengths")))
    check("strengths NOT sent to drafter (revise prompt has objections only)",
          "strength" not in planA.read_text(encoding="utf-8").lower())
    check("usage accumulated",
          rep.get("usage", {}).get("zcode", {}).get("input_tokens") == 2000,
          json.dumps(rep.get("usage", {})))

    print("scenario B: blocked-on-human then decisions resume")
    planB = make_plan(repoA, "plan-b.md", "# Plan B\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planB, [repoA, repoB], stub_mode="openq")
    check("exit code 4", rc == 4, f"rc={rc}")
    check("outcome blocked-on-human", rep and rep.get("outcome") == "blocked-on-human")
    oqf = Path(rep["open_questions_file"])
    check("open-questions.json written", oqf.is_file())
    qs = json.loads(oqf.read_text(encoding="utf-8"))
    qs[0]["answer"] = "flag on"
    dec = oqf.parent / "decisions.json"
    dec.write_text(json.dumps(qs, indent=2), encoding="utf-8")
    rc2, rep2, _ = run_engine(planB, [repoA, repoB], extra=["--decisions", str(dec)],
                              stub_mode="openq")
    check("resume exit 0", rc2 == 0, f"rc={rc2}")
    check("resume consensus", rep2 and rep2.get("outcome") == "consensus")
    check("decision recorded", rep2.get("decisions", {}).get(
        "Ship the stub feature behind a flag?") == "flag on")

    print("scenario C: implement blocked on main")
    planC = make_plan(repoA, "plan-c.md", "# Plan C\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planC, [repoA, repoB], extra=["--implement"],
                              stub_mode="approve")
    check("exit code 5", rc == 5, f"rc={rc}")
    check("outcome blocked-on-branch", rep and rep.get("outcome") == "blocked-on-branch")
    check("nothing edited", not (repoA / "stub-implement.txt").exists())
    check("blocked repo named", rep and rep.get("branch_blocked_repos"), str(rep)[:120])

    print("scenario D: implement through the junction")
    subprocess.run(["git", "checkout", "-q", "-b", "feature/stub"], cwd=repoA,
                   check=True, capture_output=True)
    planD = make_plan(repoA, "plan-d.md", "# Plan D\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planD, [repoA, repoB], extra=["--implement"],
                              stub_mode="approve")
    check("exit code 0", rc == 0, f"rc={rc}")
    check("outcome consensus+implement", rep and rep.get("outcome") == "consensus"
          and rep.get("implement_attempted") is True)
    check("file landed in REAL repo", (repoA / "stub-implement.txt").is_file())
    check("agents chose repoa", "repoa" in " ".join(rep.get("repos_touched", [])))

    print("scenario E: unparseable review re-asked, not treated as plan defect")
    planE = make_plan(repoA, "plan-e.md", "# Plan E\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planE, [repoA, repoB], stub_mode="badjson")
    check("exit code 0", rc == 0, f"rc={rc}")
    check("consensus on iteration 1 (retry did not burn it)",
          rep and rep.get("outcome") == "consensus" and rep.get("iterations_used") == 1,
          str(rep)[:120])
    check("reviewer called twice (retry happened)",
          rep.get("usage", {}).get("zcode", {}).get("input_tokens") == 2000,
          json.dumps(rep.get("usage", {})))
    check("plan untouched (no nonsense objection sent to drafter)",
          "Stub revision" not in planE.read_text(encoding="utf-8"))
    rj = json.loads((Path(rep["history_dir"]) / "review-iter-01.json").read_text(encoding="utf-8"))
    check("raw verdict persisted", "raw" in rj and rj["raw"].startswith("{"))

    print("scenario F: second concurrent run on the same plan is refused")
    planF = make_plan(repoA, "plan-f.md", "# Plan F\n\nGoal: stub goal.\n")
    histF = planA.parent / ".countersign" / "plan-f-history"
    histF.mkdir(parents=True, exist_ok=True)
    (histF / "run.lock").write_text("pid=99999 started=fake\n", encoding="utf-8")
    rc, rep, err = run_engine(planF, [repoA, repoB], stub_mode="approve")
    check("exit code 2", rc == 2, f"rc={rc}")
    check("outcome locked", rep and rep.get("outcome") == "locked", str(rep)[:120])
    (histF / "run.lock").unlink()
    rc, rep, err = run_engine(planF, [repoA, repoB], stub_mode="approve")
    check("runs again once lock cleared", rc == 0 and rep.get("outcome") == "consensus")
    check("lock released after run", not (histF / "run.lock").exists())

    # junction workspace must expose ONLY the linked repos
    ws = Path(rep["history_dir"])  # not the ws; find via ~/.countersign
    print(failures and f"\n{len(failures)} FAILURE(S): {failures}" or "\nALL SCENARIOS PASS")
    sys.exit(1 if failures else 0)
