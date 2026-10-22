#!/usr/bin/env python3
"""Delegate runner e2e test with a stub ZCode CLI - zero model spend.

Covers the delegate plan's section 4 (stub ZCode, temporary git repos,
temporary HOME so no real ~/.zcode or ~/.countersign file is touched):

  D1  happy path      : tracked-file edit + brand-new UNADDED file ->
                        outcome done, both in files[] (new one status A),
                        caller tree untouched, usage filled, config set
                        mid-run (effort high) and restored after.
  D1b no usage        : stub reply without a usage key -> usage fields null.
  D2  git shim refused: commit/push/stash, checkout main, checkout -b,
                        switch, reset --hard, branch -D, worktree add ->
                        refusal message, HEAD/branch unchanged.
  D3  git shim allowed: checkout -- file, checkout file, restore, add,
                        reset -- file, status reach the real git.
  D4  shim bypass     : a real commit through the ABSOLUTE git path ->
                        glm_committed true, work still in the diff.
  D5  quota pre-check : recorded monitor response (5h at 73%) refuses with
                        reset times; no key -> proceeds with a note.
  D6  mid-run limit   : limit wording with no --on-limit -> rate-limited
                        with the session id; resume continues that session.
  D7  on-limit wait   : sleep computed from the recorded nextResetTime
                        (scaled to ~0), one wait, then resume -> done.
  D8  config restore  : --model set+restored; stub crash restores;
                        SIGTERM restores; a stale dead-PID lock's saved
                        originals are restored first; a concurrent writer
                        is not overwritten (loud warning instead).
  D9  multi-repo      : worktree per repo, workspace cwd, scoped checks.
  D10 apply           : diff incl. the new file lands UNCOMMITTED; overlap
                        with caller changes is refused.
  D11 clean           : removes worktree+branch after apply; refuses on
                        unapplied work / post-apply edits; --force overrides.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DELEGATE = HERE.parent / "scripts" / "delegate.py"
STUB_ZCODE = HERE / "stub_zcode.py"
PY = sys.executable

failures = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run_delegate(args, home, mode, extra_env=None, cwd=None, timeout=300):
    env = dict(os.environ,
               HOME=str(home),
               CS_STUB_MODE=mode,
               CS_STUB_STATE_DIR=str(home / "stub-state"),
               CS_STUB_OBSERVE=str(home / "observed.jsonl"))
    env.pop("ZAI_API_KEY", None)
    env.update(extra_env or {})
    proc = subprocess.run([PY, str(DELEGATE), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=str(cwd or home), timeout=timeout)
    rep = None
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            rep = json.loads(line)
            break
        except ValueError:
            continue
    return proc.returncode, rep, proc.stderr, len(proc.stdout.strip().splitlines())


def make_repo(path, gitignore=False):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "s@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("base readme\n", encoding="utf-8")
    if gitignore:
        (path / ".gitignore").write_text("glm-ignored.txt\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True,
                   capture_output=True)
    return path


def seed_configs(home, cli_main="zai-coding-plan/GLM-5.3",
                 selection=None, default_variant=None):
    """Pre-seed the zcode config files inside the temp HOME (never the real one)."""
    cli = home / ".zcode" / "cli" / "config.json"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text(json.dumps({"model": {"main": cli_main}}, indent=2),
                   encoding="utf-8")
    pc = home / ".zcode" / "v2" / "provider_config.json"
    pc.parent.mkdir(parents=True, exist_ok=True)
    pc.write_text(json.dumps(
        {"schemaVersion": 1,
         "config": {"defaultModelSelection": selection
                    if selection is not None else
                    {"providerId": "account:zai-individual-coding-plan",
                     "modelId": "GLM-5.3"}}}, indent=2), encoding="utf-8")
    pc.chmod(0o600)   # owner-only, as ZCode writes it
    v2 = home / ".zcode" / "v2" / "config.json"
    models = {}
    for m in ("GLM-5.3", "GLM-5.3-Flash"):
        models[m] = {"reasoning": {"enabled": True,
                                   "variants": ["low", "high", "max"],
                                   "defaultVariant": default_variant or "low"}}
    v2.write_text(json.dumps(
        {"provider": {"builtin:zai-coding-plan": {"models": models}}},
        indent=2), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def shim_env(home, run_id):
    shim = home / ".countersign" / "delegate" / run_id / "shim"
    env = dict(os.environ)
    env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    return env, shim


with tempfile.TemporaryDirectory(prefix="cs-delegate-e2e-") as td:
    T = Path(td)

    # ---------------- D1: happy path --------------------------------------
    print("scenario D1: happy path (edit + unadded new file, usage, restore)")
    home1 = T / "home1"
    home1.mkdir()
    seed_configs(home1)
    repo1 = make_repo(T / "drepo1", gitignore=True)
    brief1 = home1 / "brief1.md"
    brief1.write_text("# Brief\n\nGoal: delegate stub task.\n", encoding="utf-8")
    # pre-write a rollout record for the deterministic stub session id
    roll = home1 / ".zcode" / "cli" / "rollout"
    roll.mkdir(parents=True, exist_ok=True)
    (roll / "model-io-1.jsonl").write_text(json.dumps({
        "sessionId": "stub-zcode-1", "model": {"modelId": "GLM-5.3"},
        "request": {"body": {"output_config": {"effort": "high"}}}}) + "\n",
        encoding="utf-8")
    rc, rep, err, nout = run_delegate(
        ["run", "--brief", str(brief1), "--repo", str(repo1),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0",
         "--check", "true"], home1, "delegate")
    check("D1 exit 0", rc == 0, f"rc={rc} err={err[-300:]}")
    check("D1 outcome done", rep and rep.get("outcome") == "done", str(rep)[:160])
    check("D1 exactly one stdout JSON line", nout == 1, f"stdout lines={nout}")
    files = {f["path"]: f for f in rep["repos"][0]["files"]}
    check("D1 tracked file listed as M",
          files.get("README.md", {}).get("status") == "M"
          and files["README.md"]["added"] >= 1, str(files.get("README.md")))
    check("D1 new unadded file listed as A",
          files.get("glm-new-file.txt", {}).get("status") == "A",
          str(files.get("glm-new-file.txt")))
    check("D1 glm_committed false", rep["repos"][0]["glm_committed"] is False)
    check("D1 caller tree untouched",
          (repo1 / "README.md").read_text(encoding="utf-8") == "base readme\n"
          and not (repo1 / "glm-new-file.txt").exists())
    check("D1 usage filled from CLI JSON",
          rep.get("usage") == {"input": 12000, "output": 600, "cache_read": 9000},
          json.dumps(rep.get("usage")))
    check("D1 model_served from rollout record",
          rep.get("model_served") == "GLM-5.3 (reasoning effort: high)",
          str(rep.get("model_served")))
    check("D1 check recorded exit 0",
          rep.get("checks") and rep["checks"][0]["exit"] == 0
          and "tail" in rep["checks"][0], json.dumps(rep.get("checks"))[:160])
    check("D1 shim warning always present",
          any("best-effort" in w for w in rep.get("warnings", [])),
          str(rep.get("warnings"))[:160])
    check("D1 gitignored new file named in warnings, not in files",
          any("glm-ignored.txt" in w for w in rep.get("warnings", []))
          and "glm-ignored.txt" not in files, str(rep.get("warnings"))[:160])
    check("D1 quota skipped with a note (no key)",
          any("quota" in w.lower() for w in rep.get("warnings", [])),
          str(rep.get("warnings"))[:200])
    # config set DURING the run (observed by the stub) + restored after
    obs = [json.loads(l) for l in (home1 / "observed.jsonl").read_text(
        encoding="utf-8").splitlines()]
    check("D1 effort high served mid-run",
          obs and obs[0].get("default_selection", {}).get("options", {})
          .get("reasoningLevel") == "high", str(obs))
    check("D1 model key untouched (no --model)",
          obs and obs[0].get("cli_model_main") == "zai-coding-plan/GLM-5.3",
          str(obs))
    check("D1 provider_config.json stays owner-only (0600) after the edits",
          (home1 / ".zcode" / "v2" / "provider_config.json").stat().st_mode & 0o777 == 0o600)
    check("D1 configs restored after the run",
          read_json(home1 / ".zcode" / "cli" / "config.json")
          ["model"]["main"] == "zai-coding-plan/GLM-5.3"
          and read_json(home1 / ".zcode" / "v2" / "provider_config.json")
          ["config"]["defaultModelSelection"].get("options") is None
          and read_json(home1 / ".zcode" / "v2" / "config.json")
          ["provider"]["builtin:zai-coding-plan"]["models"]["GLM-5.3"]
          ["reasoning"]["defaultVariant"] == "low")
    run1 = rep["run_id"]
    check("D1 no leftover config lock",
          not (home1 / ".countersign" / "zcode-config.lock").exists())
    check("D1 run dir keeps brief + report",
          (home1 / ".countersign" / "delegate" / run1 / "brief.md").is_file()
          and (home1 / ".countersign" / "delegate" / run1 / "report.json").is_file())

    # status reads the persisted state
    rc, srep, _, nout = run_delegate(["status", run1], home1, "delegate")
    check("D1 status exit 0 and reports done",
          rc == 0 and srep.get("outcome") == "done", str(srep)[:160])

    # ---------------- D2: git shim, refused forms ---------------------------
    print("scenario D2: git shim refuses history-destroying forms")
    env2, shim2 = shim_env(home1, run1)
    wt1 = rep["repos"][0]["worktree"]
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt1,
                                 capture_output=True, text=True).stdout
    refused = [
        ["commit", "-m", "x"],
        ["push"],
        ["stash"],
        ["checkout", "main"],
        ["checkout", "-b", "x"],
        ["checkout", "-"],
        ["switch", "y"],
        ["reset", "--hard"],
        ["branch", "-D", "x"],
        ["worktree", "add", str(T / "wt2"), "-b", "b2"],
    ]
    all_refused = True
    for argv in refused:
        p = subprocess.run(["git", *argv], cwd=wt1, env=env2,
                           capture_output=True, text=True)
        ok_refusal = (p.returncode != 0
                      and "refused by countersign delegate" in (p.stderr + p.stdout))
        if not ok_refusal:
            all_refused = False
            check(f"D2 refuse git {argv[0]}", False,
                  f"rc={p.returncode} out={p.stdout[:80]} err={p.stderr[:80]}")
    check("D2 all ten forms refused with the message", all_refused)
    check("D2 branch and HEAD unchanged",
          subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt1,
                         capture_output=True, text=True).stdout == head_before
          and subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                             cwd=wt1, capture_output=True,
                             text=True).stdout.strip() == f"glm/{run1}")

    # ---------------- D3: git shim, allowed forms ---------------------------
    print("scenario D3: git shim passes through allowed forms")
    readme = Path(wt1) / "README.md"
    base_txt = "base readme\n"

    def shim(*argv):
        return subprocess.run(["git", *argv], cwd=wt1, env=env2,
                              capture_output=True, text=True)

    readme.write_text("dirty1\n", encoding="utf-8")
    p = shim("checkout", "--", "README.md")
    check("D3 checkout -- path restores", p.returncode == 0
          and readme.read_text(encoding="utf-8") == base_txt,
          f"rc={p.returncode} {p.stderr[:100]}")
    readme.write_text("dirty2\n", encoding="utf-8")
    p = shim("checkout", "README.md")
    check("D3 checkout <existing path> restores", p.returncode == 0
          and readme.read_text(encoding="utf-8") == base_txt,
          f"rc={p.returncode} {p.stderr[:100]}")
    readme.write_text("dirty3\n", encoding="utf-8")
    p = shim("restore", "README.md")
    check("D3 restore restores", p.returncode == 0
          and readme.read_text(encoding="utf-8") == base_txt,
          f"rc={p.returncode} {p.stderr[:100]}")
    readme.write_text("staged\n", encoding="utf-8")
    p = shim("add", "README.md")
    staged = "M  README.md" in shim("status", "--porcelain").stdout
    check("D3 add stages", p.returncode == 0 and staged)
    p = shim("reset", "README.md")
    unstaged = " M README.md" in shim("status", "--porcelain").stdout
    check("D3 reset -- path unstages", p.returncode == 0 and unstaged)
    p = shim("status")
    check("D3 status passes through", p.returncode == 0 and "README" in p.stdout)
    p = shim("diff", "HEAD")
    check("D3 read-only diff passes through", p.returncode == 0
          and "README.md" in p.stdout and "+staged" in p.stdout,
          f"rc={p.returncode} {p.stdout[:80]}")
    shim("checkout", "--", "README.md")   # leave the worktree tidy for D10
    # re-establish D1's delegated edit (the path-restore probes above undid
    # it) so apply/clean exercise the full diff
    (Path(wt1) / "README.md").write_text("base readme\ndelegated line\n",
                                         encoding="utf-8")

    # ---------------- D4: shim bypass via absolute git ----------------------
    print("scenario D4: absolute-path git commit still detected")
    home4 = T / "home4"
    home4.mkdir()
    seed_configs(home4)
    repo4 = make_repo(T / "drepo4")
    brief4 = home4 / "brief4.md"
    brief4.write_text("# Brief\n\nGoal: commit bypass.\n", encoding="utf-8")
    rc, rep4, err4, nout = run_delegate(
        ["run", "--brief", str(brief4), "--repo", str(repo4),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0"],
        home4, "delegate_commit", extra_env={"CS_REAL_GIT": "/usr/bin/git"})
    check("D4 exit 0 / done", rc == 0 and rep4.get("outcome") == "done",
          f"rc={rc} {str(rep4)[:120]}")
    check("D4 glm_committed true", rep4["repos"][0]["glm_committed"] is True)
    files4 = {f["path"] for f in rep4["repos"][0]["files"]}
    check("D4 committed work still in the diff",
          "README.md" in files4 and "glm-new-file.txt" in files4, str(files4))
    run4 = rep4["run_id"]

    # ---------------- D5: quota pre-check -----------------------------------
    print("scenario D5: usage pre-check refuses at/over the start share")
    home5 = T / "home5"
    home5.mkdir()
    seed_configs(home5)
    repo5 = make_repo(T / "drepo5")
    brief5 = home5 / "brief5.md"
    brief5.write_text("# Brief\n\nGoal: quota.\n", encoding="utf-8")
    reset_5h = int(time.time() * 1000) + 3600_000
    reset_wk = int(time.time() * 1000) + 86_400_000
    quota_file = home5 / "quota73.json"
    quota_file.write_text(json.dumps({"data": {"limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 73,
         "nextResetTime": reset_5h},
        {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 40,
         "nextResetTime": reset_wk},
    ]}}), encoding="utf-8")
    rc, rep5, err5, nout = run_delegate(
        ["run", "--brief", str(brief5), "--repo", str(repo5),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0",
         "--quota-endpoint", f"file://{quota_file}"],
        home5, "delegate", extra_env={"ZAI_API_KEY": "test-key"})
    check("D5 exit nonzero / refused-by-quota",
          rc != 0 and rep5.get("outcome") == "rate-limited",
          f"rc={rc} {str(rep5)[:160]}")
    check("D5 both reset times carried",
          rep5.get("quota", {}).get("resets", {}).get("five_hour") == reset_5h
          and rep5["quota"]["resets"].get("weekly") == reset_wk,
          json.dumps(rep5.get("quota", {}).get("resets")))
    check("D5 no worktree created", not rep5.get("repos"))

    print("scenario D5b: healthy quota passes, recorded before and after")
    quota_ok = home5 / "quota-ok.json"
    quota_ok.write_text(json.dumps({"data": {"limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 20,
         "nextResetTime": reset_5h},
        {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 40},
    ]}}), encoding="utf-8")
    rc, rep5b, err5b, nout = run_delegate(
        ["run", "--brief", str(brief5), "--repo", str(repo5),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0",
         "--quota-endpoint", f"file://{quota_ok}"],
        home5, "delegate", extra_env={"ZAI_API_KEY": "test-key"})
    check("D5b run proceeds (done)",
          rc == 0 and rep5b.get("outcome") == "done", f"rc={rc}")
    check("D5b quota shares recorded before and after",
          rep5b.get("quota", {}).get("before", {}).get("five_hour", {})
          .get("share") == 0.2
          and rep5b["quota"]["after"]["five_hour"]["share"] == 0.2,
          json.dumps(rep5b.get("quota"))[:200])

    # ---------------- D6: mid-run limit, return -----------------------------
    print("scenario D6: mid-run limit returns with session id; resume continues")
    home6 = T / "home6"
    home6.mkdir()
    seed_configs(home6)
    repo6 = make_repo(T / "drepo6")
    brief6 = home6 / "brief6.md"
    brief6.write_text("# Brief\n\nGoal: limit.\n", encoding="utf-8")
    rc, rep6, err6, nout = run_delegate(
        ["run", "--brief", str(brief6), "--repo", str(repo6),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0"],
        home6, "delegate_limit")
    check("D6 default --on-limit return: rate-limited",
          rep6.get("outcome") == "rate-limited" and rc != 0,
          f"rc={rc} {str(rep6)[:160]}")
    check("D6 session id reported", rep6.get("session_id") == "stub-zcode-1",
          str(rep6.get("session_id")))
    run6 = rep6["run_id"]
    rc, rep6b, err6b, nout = run_delegate(
        ["resume", run6, "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0"],
        home6, "delegate_limit")
    check("D6 resume continues the same session and finishes",
          rc == 0 and rep6b.get("outcome") == "done"
          and rep6b.get("session_id") == "stub-zcode-2", str(rep6b)[:160])
    resumes6 = ((home6 / "stub-state" / "cs-stub-state")
                .glob("*.resumes"))
    all_resumes = " ".join(p.read_text(encoding="utf-8")
                           for p in resumes6)
    check("D6 stub saw --resume with the recorded session id",
          "stub-zcode-1" in all_resumes, all_resumes[:120])

    # ---------------- D7: on-limit wait -------------------------------------
    print("scenario D7: on-limit wait sleeps to nextResetTime+3min, then resumes")
    home7 = T / "home7"
    home7.mkdir()
    seed_configs(home7)
    repo7 = make_repo(T / "drepo7")
    brief7 = home7 / "brief7.md"
    brief7.write_text("# Brief\n\nGoal: wait.\n", encoding="utf-8")
    reset7 = int((time.time() + 120) * 1000)      # 2 minutes out
    quota7 = home7 / "quota-wait.json"
    quota7.write_text(json.dumps({"data": {"limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 20,
         "nextResetTime": reset7},
    ]}}), encoding="utf-8")
    rc, rep7, err7, nout = run_delegate(
        ["run", "--brief", str(brief7), "--repo", str(repo7),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0",
         "--on-limit", "wait", "--quota-endpoint", f"file://{quota7}"],
        home7, "delegate_limit",
        extra_env={"CS_DELEGATE_WAIT_SCALE": "0", "ZAI_API_KEY": "test-key"})
    check("D7 waited once, then done",
          rc == 0 and rep7.get("outcome") == "done"
          and rep7.get("waits_used") == 1, f"rc={rc} {str(rep7)[:160]}")
    check("D7 wait computed from nextResetTime (not a fixed interval)",
          290 <= rep7.get("wait_seconds_total", -1) <= 320,
          str(rep7.get("wait_seconds_total")))
    check("D7 resumed the same session", rep7.get("session_id") == "stub-zcode-2",
          str(rep7.get("session_id")))

    # ---------------- D8: config restore edge cases -------------------------
    print("scenario D8: model pin, crash restore, SIGTERM restore, stale lock,")
    print("               concurrent writer")
    home8 = T / "home8"
    home8.mkdir()
    seed_configs(home8)
    repo8 = make_repo(T / "drepo8")
    brief8 = home8 / "brief8.md"
    brief8.write_text("# Brief\n\nGoal: model pin.\n", encoding="utf-8")
    rc, rep8, err8, nout = run_delegate(
        ["run", "--brief", str(brief8), "--repo", str(repo8),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0",
         "--model", "GLM-5.3-Flash"],
        home8, "delegate")
    obs8 = [json.loads(l) for l in (home8 / "observed.jsonl").read_text(
        encoding="utf-8").splitlines()]
    check("D8 --model applied mid-run (cli pin + selection)",
          obs8 and obs8[0].get("cli_model_main") == "zai-coding-plan/GLM-5.3-Flash"
          and obs8[0].get("default_selection", {}).get("modelId") == "GLM-5.3-Flash"
          and obs8[0]["default_selection"]["options"]["reasoningLevel"] == "high",
          str(obs8))
    check("D8 --model restored after the run",
          read_json(home8 / ".zcode" / "cli" / "config.json")["model"]["main"]
          == "zai-coding-plan/GLM-5.3")

    # stub crash: originals still restored
    home8b = T / "home8b"
    home8b.mkdir()
    seed_configs(home8b)
    repo8b = make_repo(T / "drepo8b")
    brief8b = home8b / "brief8b.md"
    brief8b.write_text("# Brief\n\nGoal: crash.\n", encoding="utf-8")
    rc, rep8b, err8b, nout = run_delegate(
        ["run", "--brief", str(brief8b), "--repo", str(repo8b),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0"],
        home8b, "delegate_fail")
    check("D8b stub failure -> outcome failed", rc != 0
          and rep8b.get("outcome") == "failed", f"rc={rc} {str(rep8b)[:120]}")
    check("D8b configs restored after the crash",
          read_json(home8b / ".zcode" / "cli" / "config.json")["model"]["main"]
          == "zai-coding-plan/GLM-5.3"
          and read_json(home8b / ".zcode" / "v2" / "provider_config.json")
          ["config"]["defaultModelSelection"].get("options") is None)

    # SIGTERM mid-call: originals restored, one JSON line, lock released
    if os.name != "nt":
        home8c = T / "home8c"
        home8c.mkdir()
        seed_configs(home8c)
        repo8c = make_repo(T / "drepo8c")
        brief8c = home8c / "brief8c.md"
        brief8c.write_text("# Brief\n\nGoal: hang.\n", encoding="utf-8")
        envc = dict(os.environ, HOME=str(home8c), CS_STUB_MODE="delegate_hang",
                    CS_STUB_STATE_DIR=str(home8c / "stub-state"),
                    CS_STUB_OBSERVE=str(home8c / "observed.jsonl"))
        envc.pop("ZAI_API_KEY", None)
        procc = subprocess.Popen(
            [PY, str(DELEGATE), "run", "--brief", str(brief8c),
             "--repo", str(repo8c), "--zcode-cli", str(STUB_ZCODE),
             "--heartbeat", "0", "--timeout", "60"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=envc, cwd=str(home8c))
        lockc = home8c / ".countersign" / "zcode-config.lock"
        for _ in range(200):
            if lockc.exists():
                break
            time.sleep(0.1)
        check("D8c config lock held mid-run", lockc.exists())
        procc.terminate()
        outc, _ = procc.communicate(timeout=30)
        repc = None
        for line in reversed(outc.strip().splitlines()):
            try:
                repc = json.loads(line)
                break
            except ValueError:
                continue
        check("D8c interrupted outcome on stdout", repc
              and repc.get("outcome") == "interrupted", str(repc)[:120])
        check("D8c configs restored after SIGTERM",
              read_json(home8c / ".zcode" / "cli" / "config.json")
              ["model"]["main"] == "zai-coding-plan/GLM-5.3")
        check("D8c lock released", not lockc.exists())

    # stale lock with a dead PID: saved originals restored before applying
    home8d = T / "home8d"
    home8d.mkdir()
    seed_configs(home8d)
    repo8d = make_repo(T / "drepo8d")
    brief8d = home8d / "brief8d.md"
    brief8d.write_text("# Brief\n\nGoal: stale lock.\n", encoding="utf-8")
    cs_dir = home8d / ".countersign"
    cs_dir.mkdir(parents=True, exist_ok=True)
    dead = subprocess.Popen([PY, "-c", "pass"])
    dead.wait()
    (cs_dir / "zcode-config.lock").write_text(
        f"pid={dead.pid} started=2020-01-01T00:00:00+00:00\n", encoding="utf-8")
    # the dead run "wrote" WRITTEN-BY-DEAD over original ORIG-FROM-DEAD
    cli_d = home8d / ".zcode" / "cli" / "config.json"
    cli_d.write_text(json.dumps({"model": {"main": "WRITTEN-BY-DEAD"}}),
                     encoding="utf-8")
    (cs_dir / "zcode-config.state.json").write_text(json.dumps({
        "entries": [{"file": str(cli_d), "key": ["model", "main"],
                     "existed": True, "original": "ORIG-FROM-DEAD",
                     "written": "WRITTEN-BY-DEAD"}]}), encoding="utf-8")
    rc, rep8d, err8d, nout = run_delegate(
        ["run", "--brief", str(brief8d), "--repo", str(repo8d),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0"],
        home8d, "delegate")
    check("D8d stale-lock run succeeds", rc == 0
          and rep8d.get("outcome") == "done", f"rc={rc} {str(rep8d)[:120]}")
    check("D8d dead run's original restored (not its written value)",
          read_json(cli_d)["model"]["main"] == "ORIG-FROM-DEAD",
          read_json(cli_d)["model"]["main"])

    # concurrent writer: changed key is NOT overwritten; loud warning
    home8e = T / "home8e"
    home8e.mkdir()
    seed_configs(home8e)
    repo8e = make_repo(T / "drepo8e")
    brief8e = home8e / "brief8e.md"
    brief8e.write_text("# Brief\n\nGoal: concurrent.\n", encoding="utf-8")
    rc, rep8e, err8e, nout = run_delegate(
        ["run", "--brief", str(brief8e), "--repo", str(repo8e),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0"],
        home8e, "delegate_concurrent")
    check("D8e concurrent run still done", rc == 0
          and rep8e.get("outcome") == "done", f"rc={rc}")
    sel_e = read_json(home8e / ".zcode" / "v2" / "provider_config.json")[
        "config"]["defaultModelSelection"]
    check("D8e changed key left as the other writer set it",
          sel_e == {"providerId": "account:other-plan", "modelId": "GLM-5.3"},
          json.dumps(sel_e))
    check("D8e loud warning names key, found value and skipped original",
          any("defaultModelSelection" in w and "account:other-plan" in w
              for w in rep8e.get("warnings", [])),
          str(rep8e.get("warnings"))[:240])

    # ---------------- D9: multi-repo workspace ------------------------------
    print("scenario D9: two repos -> two worktrees, workspace cwd, scoped checks")
    home9 = T / "home9"
    home9.mkdir()
    seed_configs(home9)
    repo9a = make_repo(T / "drepo9a")
    repo9b = make_repo(T / "drepo9b")
    brief9 = home9 / "brief9.md"
    brief9.write_text("# Brief\n\nGoal: multi.\n", encoding="utf-8")
    rc, rep9, err9, nout = run_delegate(
        ["run", "--brief", str(brief9), "--repo", str(repo9a),
         "--repo", str(repo9b), "--zcode-cli", str(STUB_ZCODE),
         "--heartbeat", "0",
         "--check", "true", "--check", "drepo9b:false"],
        home9, "delegate")
    check("D9 done", rc == 0 and rep9.get("outcome") == "done",
          f"rc={rc} {str(rep9)[:160]}")
    check("D9 two worktrees recorded", len(rep9.get("repos", [])) == 2)
    wt9a = Path(rep9["repos"][0]["worktree"])
    check("D9 edits landed in the worktree, not the caller tree",
          "delegated line" in (wt9a / "README.md").read_text(encoding="utf-8")
          and (repo9a / "README.md").read_text(encoding="utf-8") == "base readme\n")
    check("D9 unprefixed check ran per worktree, scoped check only in drepo9b",
          len(rep9.get("checks", [])) == 3
          and sum(1 for c in rep9["checks"] if c["exit"] == 1) == 1,
          json.dumps(rep9.get("checks"))[:240])

    # ---------------- D10: apply --------------------------------------------
    print("scenario D10: apply lands the diff uncommitted; overlap refused")
    rc, arep, aerr, nout = run_delegate(["apply", run1], home1, "delegate")
    check("D10 apply exit 0", rc == 0 and arep.get("outcome") == "done",
          f"rc={rc} {str(arep)[:160]}")
    check("D10 README modified in caller",
          "delegated line" in (repo1 / "README.md").read_text(encoding="utf-8"))
    st1 = subprocess.run(["git", "status", "--porcelain"], cwd=repo1,
                         capture_output=True, text=True).stdout
    check("D10 new file present (untracked or intent-to-add)",
          (repo1 / "glm-new-file.txt").is_file()
          and ("?? glm-new-file.txt" in st1 or "A  glm-new-file.txt" in st1),
          st1)
    head1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo1,
                           capture_output=True, text=True).stdout
    check("D10 nothing committed by apply", subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo1, capture_output=True,
        text=True).stdout == head1)
    check("D10 apply leaves the caller's index alone (nothing staged)",
          subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=repo1,
                         capture_output=True, text=True).stdout.strip() == "")
    apply_rec = home1 / ".countersign" / "delegate" / run1 / "apply.json"
    check("D10 apply record with sha256 + paths",
          apply_rec.is_file() and read_json(apply_rec)[0].get("sha256")
          and read_json(apply_rec)[0].get("paths"), str(apply_rec))

    # overlap: a fresh run whose paths the caller then dirties -> refused
    home10 = T / "home10"
    home10.mkdir()
    seed_configs(home10)
    repo10 = make_repo(T / "drepo10")
    brief10 = home10 / "brief10.md"
    brief10.write_text("# Brief\n\nGoal: overlap.\n", encoding="utf-8")
    rc, rep10, _, nout = run_delegate(
        ["run", "--brief", str(brief10), "--repo", str(repo10),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0"],
        home10, "delegate")
    run10 = rep10["run_id"]
    (repo10 / "README.md").write_text("caller-dirtied\n", encoding="utf-8")
    rc, rep10a, err10a, nout = run_delegate(["apply", run10], home10, "delegate")
    check("D10 overlap refused without --force",
          rc != 0 and rep10a.get("outcome") == "refused"
          and "README.md" in str(rep10a.get("error", "")),
          f"rc={rc} {str(rep10a)[:160]}")
    subprocess.run(["git", "checkout", "--", "README.md"], cwd=repo10,
                   capture_output=True)
    rc, rep10b, err10b, nout = run_delegate(["apply", run10, "--force"], home10,
                                      "delegate")
    check("D10 --force applies once the overlap is resolved",
          rc == 0 and rep10b.get("outcome") == "done", f"rc={rc}")

    # ---------------- D11: clean --------------------------------------------
    print("scenario D11: clean after apply; refuses unapplied/edited work")
    rc, crep, cerr, nout = run_delegate(["clean", run1], home1, "delegate")
    check("D11 clean after apply removes worktree + branch",
          rc == 0 and crep.get("outcome") == "done", f"rc={rc} {str(crep)[:160]}")
    check("D11 worktree gone", not Path(wt1).exists())
    check("D11 glm branch gone", f"glm/{run1}" not in subprocess.run(
        ["git", "branch", "--list"], cwd=repo1, capture_output=True,
        text=True).stdout)

    # unapplied work: only an untracked new file, no apply record -> refuse
    home11 = T / "home11"
    home11.mkdir()
    seed_configs(home11)
    repo11 = make_repo(T / "drepo11")
    brief11 = home11 / "brief11.md"
    brief11.write_text("# Brief\n\nGoal: clean refuse.\n", encoding="utf-8")
    rc, rep11, _, nout = run_delegate(
        ["run", "--brief", str(brief11), "--repo", str(repo11),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0"],
        home11, "delegate")
    run11 = rep11["run_id"]
    rc, crep11, cerr11, nout = run_delegate(["clean", run11], home11, "delegate")
    check("D11 unapplied work refuses and names paths",
          rc != 0 and crep11.get("outcome") == "refused"
          and "glm-new-file.txt" in str(crep11.get("error", "")),
          f"rc={rc} {str(crep11)[:200]}")
    check("D11 worktree kept after refusal", Path(rep11["repos"][0]["worktree"]).is_dir())
    rc, crep11f, _, nout = run_delegate(["clean", run11, "--force"], home11, "delegate")
    check("D11 --force removes anyway", rc == 0
          and not Path(rep11["repos"][0]["worktree"]).exists())

    # edited after apply -> hash mismatch -> refuse
    home11b = T / "home11b"
    home11b.mkdir()
    seed_configs(home11b)
    repo11b = make_repo(T / "drepo11b")
    brief11b = home11b / "brief11b.md"
    brief11b.write_text("# Brief\n\nGoal: edited after apply.\n", encoding="utf-8")
    rc, rep11b, _, nout = run_delegate(
        ["run", "--brief", str(brief11b), "--repo", str(repo11b),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0"],
        home11b, "delegate")
    run11b = rep11b["run_id"]
    run_delegate(["apply", run11B := run11b], home11b, "delegate")
    # an edit AFTER apply changes the worktree diff hash
    (Path(rep11b["repos"][0]["worktree"]) / "README.md").write_text(
        "post-apply edit\n", encoding="utf-8")
    rc, crep11b, _, nout = run_delegate(["clean", run11b], home11b, "delegate")
    check("D11 post-apply edit refuses clean",
          rc != 0 and crep11b.get("outcome") == "refused",
          f"rc={rc} {str(crep11b)[:160]}")

    # ---------------- misc: no repo, bad effort ------------------------------
    print("scenario M: no git repo + invalid effort refused")
    rc, repm, errm, nout = run_delegate(
        ["run", "--brief", str(brief1), "--zcode-cli", str(STUB_ZCODE),
         "--heartbeat", "0"], T / "home1", "delegate")
    check("M no repo -> refused", rc != 0 and repm.get("outcome") == "refused",
          f"rc={rc} {str(repm)[:120]}")
    rc, repm2, errm2, nout = run_delegate(
        ["run", "--brief", str(brief1), "--repo", str(repo1),
         "--zcode-cli", str(STUB_ZCODE), "--heartbeat", "0",
         "--effort", "ultra"], T / "home1", "delegate")
    check("M effort not in catalog refused with the variants named",
          rc != 0 and repm2.get("outcome") == "refused"
          and "ultra" in str(repm2.get("error", ""))
          and "high" in str(repm2.get("error", "")),
          f"rc={rc} {str(repm2)[:160]}")

    print(failures and f"\n{len(failures)} FAILURE(S): {failures}"
          or "\nALL DELEGATE SCENARIOS PASS")
    sys.exit(1 if failures else 0)
