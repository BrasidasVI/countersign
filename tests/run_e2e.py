#!/usr/bin/env python3
"""End-to-end engine test with stub agents - zero model spend.

Scenarios (sequential, each with its own plan file and fake repos):
  A. happy loop        : revise -> revise -> approve => consensus, exit 0
  B. open questions    : approve+question => exit 4; --decisions rerun => consensus
  C. branch block      : approve + --implement, repo on main => exit 5, no edits
  D. implement         : same but repo on feature branch => exit 0, file lands
                         in the REAL repo through the junction workspace
  I. approve-with-minors: approve + minor suggestion => NOT consensus yet, one
                         more revise round, then consensus; the suggestion
                         reaches the drafter and the review artifact
  J. absent plan repo   : plan's repo not among --link-repos => run continues
                         but a loud warning lands in the report (stale-config
                         guard; implement would otherwise target the wrong repos)
  K. cumulative decisions: round 1 asks Q1+Q2; round 2's decisions.json is a
                         DELTA answering only Q1; reviewer re-asks both; round
                         3 answers only Q2 => engine must still hold Q1 settled,
                         reach a revise that bakes BOTH into the plan, consensus
  L. truncation recovery : first revise reply is a tiny prefix of the plan =>
                         refused, re-asked once, full revision accepted; content
                         preserved; consensus
  M. hard truncation    : EVERY revise reply truncated => outcome
                         revise-truncated, plan file byte-identical to the
                         original, nothing snapshotted as a revision
  N. fork plumbing      : --fork-session-id (now the default mode) => first
                         revise call forks, session chains, fork recorded in
                         the report and stderr; consensus still reached
  O. nonce fork target  : --fork-invocation-nonce => the engine forks the
                         transcript CONTAINING the nonce, even when a
                         different (newer-mtime) transcript exists in the
                         same project directory
"""
import json
import os
import re
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


def run_engine(plan, repos, extra=(), stub_mode="revise", no_fork=True,
               env_extra=None):
    env = dict(os.environ,
               CS_STUB_MODE=stub_mode,
               CS_STUB_STATE_DIR=str(STATE_DIR),
               CS_DISABLE_POWERSHELL_JUNCTION="1")  # unused; placeholder symmetry
    env.update(env_extra or {})
    cmd = [PY, str(ENGINE), str(plan),
           "--claude-cli", str(STUB_CLAUDE), "--zcode-cli", str(STUB_ZCODE),
           "--heartbeat", "0", "--max-retries", "0"]
    if no_fork:
        # keeps the suite hermetic: the engine's fork-by-default would
        # otherwise auto-discover REAL session transcripts on this machine
        cmd += ["--no-fork"]
    cmd += list(extra)
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
    check("no objections remain on consensus",
          rep.get("blocking_remaining") == [] and rep.get("minor_remaining") == [],
          str(rep)[:160])
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
    check("raw persisted untruncated (>4000 chars)",
          len(rj["raw"]) > 4000, str(len(rj["raw"])))

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

    print("scenario G: stale/wrong-branch plan refused via --expect-sha256")
    planG = make_plan(repoA, "plan-g.md", "# Plan G\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planG, [repoA, repoB], extra=["--expect-sha256", "0" * 64],
                              stub_mode="approve")
    check("exit code 2", rc == 2, f"rc={rc}")
    check("outcome plan-mismatch", rep and rep.get("outcome") == "plan-mismatch",
          str(rep)[:120])
    check("no review performed", rep.get("iterations_used") == 0)
    import hashlib as _h
    good = _h.sha256(planG.read_bytes()).hexdigest()
    rc, rep, err = run_engine(planG, [repoA, repoB], extra=["--expect-sha256", good],
                              stub_mode="approve")
    check("correct hash runs", rc == 0 and rep.get("outcome") == "consensus")
    check("hash + git context in report",
          rep.get("plan_sha256") == good and rep.get("plan_branch") in ("main", "dev", "feature/stub"))

    print("scenario H: plan in a stale worktree of a linked repo warns")
    import subprocess as _sp
    wh = T / "wh-main"
    wh.mkdir()
    _sp.run(["git", "init", "-q", "-b", "dev"], cwd=wh, check=True, capture_output=True)
    (wh / "p.md").write_text("# v1\n", encoding="utf-8")
    (wh / "README.md").write_text("x\n", encoding="utf-8")
    _sp.run(["git", "add", "-A"], cwd=wh, check=True, capture_output=True)
    _sp.run(["git", "-c", "user.email=s@t", "-c", "user.name=t", "commit", "-q", "-m", "v1"],
            cwd=wh, check=True, capture_output=True)
    (wh / "p.md").write_text("# v2 with more content\n", encoding="utf-8")
    _sp.run(["git", "-c", "user.email=s@t", "-c", "user.name=t", "commit", "-q", "-am", "v2"],
            cwd=wh, check=True, capture_output=True)
    wt = T / "wh-old"
    _sp.run(["git", "-C", str(wh), "worktree", "add", "-q", "--detach", str(wt), "HEAD~1"],
            check=True, capture_output=True)
    stale_plan = wt / "p.md"                       # v1, content INTENTIONALLY matches the hash guard
    import hashlib as _h2
    good = _h2.sha256(stale_plan.read_bytes()).hexdigest()
    rc, rep, err = run_engine(stale_plan, [wh, repoB],
                              extra=["--expect-sha256", good], stub_mode="approve")
    check("runs (warning, not block)", rc == 0 and rep.get("outcome") == "consensus",
          f"rc={rc}")
    check("worktree mismatch warned",
          any("different checkouts" in w for w in rep.get("warnings", [])),
          str(rep.get("warnings"))[:160])

    print("scenario I: approve-with-minors gets one more revise round")
    planI = make_plan(repoA, "plan-i.md", "# Plan I\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planI, [repoA, repoB], stub_mode="approveminors")
    check("exit code 0", rc == 0, f"rc={rc}")
    check("approve-with-minors did not end the loop (consensus on iteration 2)",
          rep and rep.get("outcome") == "consensus" and rep.get("iterations_used") == 2,
          str(rep)[:160])
    check("no minors remain on consensus", rep.get("minor_remaining") == [])
    check("reviewer suggestion reached the drafter",
          "spell out the retry policy" in planI.read_text(encoding="utf-8"))
    rj1 = json.loads((Path(rep["history_dir"]) / "review-iter-01.json").read_text(encoding="utf-8"))
    check("suggestion persisted in review artifact",
          rj1["minor"][0].get("suggestion") == "stub suggestion: spell out the retry policy",
          json.dumps(rj1["minor"]))

    print("scenario J: plan repo absent from the workspace warns (stale config guard)")
    repoC = T / "repoc"
    make_repo(repoC, "dev")
    planJ = make_plan(repoC, "plan-j.md", "# Plan J\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planJ, [repoA, repoB], stub_mode="approve")
    check("still runs (warning, not block)",
          rc == 0 and rep.get("outcome") == "consensus", f"rc={rc}")
    check("absence warned",
          any("plan repo not in workspace" in w for w in rep.get("warnings", [])),
          str(rep.get("warnings"))[:160])

    print("scenario K: decisions accumulate across blocked-on-human rounds")
    planK = make_plan(repoA, "plan-k.md", "# Plan K\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planK, [repoA, repoB], stub_mode="openq2")
    check("round 1 blocked", rc == 4 and rep.get("outcome") == "blocked-on-human",
          f"rc={rc}")
    qs = json.loads(Path(rep["open_questions_file"]).read_text(encoding="utf-8"))
    check("round 1 asked both questions", len(qs) == 2, str(len(qs)))
    # Round 2: the session answers ONLY Q1 (decisions.json as a delta); the
    # reviewer returns BOTH questions again - the engine must filter Q1 as
    # settled and block with just Q2, without forgetting Q1.
    qs[0]["answer"] = "free tier"
    dec1 = Path(rep["history_dir"]) / "decisions.json"
    dec1.write_text(json.dumps(qs[:1], indent=2), encoding="utf-8")
    rc, rep, err = run_engine(planK, [repoA, repoB],
                              extra=["--decisions", str(dec1)], stub_mode="openq2")
    check("round 2 blocked", rc == 4 and rep.get("outcome") == "blocked-on-human",
          f"rc={rc}")
    qs2 = json.loads(Path(rep["open_questions_file"]).read_text(encoding="utf-8"))
    check("settled question NOT re-asked",
          len(qs2) == 1 and "Q2" in qs2[0]["question"], json.dumps(qs2)[:120])
    check("round 1 answer still settled",
          rep.get("decisions", {}).get("Q1: ship the stub feature free or Pro-only?")
          == "free tier",
          json.dumps(rep.get("decisions"))[:160])
    # Round 3: the session answers ONLY Q2 as a fresh delta file - the exact
    # spot where the old engine lost Q1. Merge must hold BOTH; the revise pass
    # then bakes them into the plan before consensus.
    qs2[0]["answer"] = "dark mode ships"
    dec2 = Path(rep["history_dir"]) / "decisions-2.json"
    dec2.write_text(json.dumps(qs2, indent=2), encoding="utf-8")
    rc, rep, err = run_engine(planK, [repoA, repoB],
                              extra=["--decisions", str(dec2)], stub_mode="openq2")
    check("round 3 consensus after revise", rc == 0
          and rep.get("outcome") == "consensus" and rep.get("iterations_used") == 2,
          f"rc={rc}")
    check("decisions cumulative in report", len(rep.get("decisions", {})) == 2,
          json.dumps(rep.get("decisions"))[:200])
    check("plan revised with BOTH decisions",
          "free tier" in planK.read_text(encoding="utf-8")
          and "dark mode ships" in planK.read_text(encoding="utf-8"))
    settled_file = json.loads((Path(rep["history_dir"]) / "settled-decisions.json")
                              .read_text(encoding="utf-8"))
    check("settled-decisions.json persisted with both answers",
          len(settled_file) == 2, json.dumps(settled_file)[:160])

    print("scenario L: truncated revise reply refused, re-ask recovers")
    planL = make_plan(repoA, "plan-l.md", "# Plan L\n\nGoal: stub goal.\n")
    planL_text = planL.read_text(encoding="utf-8")
    rc, rep, err = run_engine(planL, [repoA, repoB], stub_mode="truncate")
    check("consensus after truncation recovery", rc == 0
          and rep.get("outcome") == "consensus" and rep.get("iterations_used") == 2,
          f"rc={rc}")
    finalL = planL.read_text(encoding="utf-8")
    check("full revision accepted on re-ask", "Stub revision" in finalL)
    check("original content preserved (no silent shrink)",
          all(ln in finalL for ln in planL_text.strip().splitlines()))
    check("drafter called twice (refused reply + re-ask)",
          rep.get("usage", {}).get("claude", {}).get("input_tokens") == 1000,
          json.dumps(rep.get("usage", {})))

    print("scenario M: persistent truncation refuses without corrupting")
    planM = make_plan(repoA, "plan-m.md", "# Plan M\n\nGoal: stub goal.\n")
    planM_text = planM.read_text(encoding="utf-8")
    rc, rep, err = run_engine(planM, [repoA, repoB], stub_mode="truncate2")
    check("exit code 2", rc == 2, f"rc={rc}")
    check("outcome revise-truncated", rep and rep.get("outcome") == "revise-truncated",
          str(rep)[:120])
    check("error names the truncation", "truncat" in str(rep.get("error", "")).lower())
    check("plan file byte-identical (not corrupted)",
          planM.read_text(encoding="utf-8") == planM_text)
    check("no revision snapshot written",
          not list(Path(rep["history_dir"]).glob("plan-v*.md")))

    print("scenario N: forked-conversation revise calls (default mode plumbing)")
    planN = make_plan(repoA, "plan-n.md", "# Plan N\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planN, [repoA, repoB],
                              extra=["--fork-session-id", "sess-fork-abc123"],
                              stub_mode="revise", no_fork=False)
    check("fork run reaches consensus", rc == 0 and rep.get("outcome") == "consensus",
          f"rc={rc}")
    check("fork recorded in report", rep.get("forked_from_session") == "sess-fork-abc123",
          str(rep.get("forked_from_session")))
    check("fork logged on stderr", "forked from" in err)
    check("plan revised through the fork",
          "Stub revision" in planN.read_text(encoding="utf-8"))

    print("scenario O: fork target derived from invocation nonce, not mtime")
    fake_home = T / "fakehome"
    proj = (fake_home / ".claude" / "projects"
            / re.sub(r"[^A-Za-z0-9-]", "-", str(Path.cwd())))
    proj.mkdir(parents=True, exist_ok=True)
    current = proj / "bbbb-current.jsonl"     # the chat that invoked the run
    current.write_text(
        json.dumps({"sessionId": "sess-NONCE-0002"}) + "\n"
        + json.dumps({"tool_use": "countersign --fork-invocation-nonce cs-nonce-42"}) + "\n",
        encoding="utf-8")
    import time as _time
    old = _time.time() - 3600
    os.utime(current, (old, old))            # older: mtime alone would NOT pick it
    stale = proj / "aaaa-stale.jsonl"        # newest by mtime, but NOT the invoker
    stale.write_text(json.dumps({"sessionId": "sess-STALE-0001"}) + "\n",
                     encoding="utf-8")
    planO = make_plan(repoA, "plan-o.md", "# Plan O\n\nGoal: stub goal.\n")
    rc, rep, err = run_engine(planO, [repoA, repoB],
                              extra=["--fork-invocation-nonce", "cs-nonce-42"],
                              stub_mode="revise", no_fork=False,
                              env_extra={"HOME": str(fake_home)})
    check("nonce run reaches consensus", rc == 0
          and rep.get("outcome") == "consensus", f"rc={rc}")
    check("forked the NONCE session (not the newest transcript)",
          rep.get("forked_from_session") == "sess-NONCE-0002",
          str(rep.get("forked_from_session")))
    check("nonce match logged", "nonce" in err.lower())

    print(failures and f"\n{len(failures)} FAILURE(S): {failures}" or "\nALL SCENARIOS PASS")
    sys.exit(1 if failures else 0)
