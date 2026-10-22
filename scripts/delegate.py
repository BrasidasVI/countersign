#!/usr/bin/env python3
"""delegate - run one well-specified task through the headless ZCode CLI (GLM).

The countersign plugin's second skill: a Claude Code session hands a
well-specified implementation task to GLM, so the output tokens are spent on
the z.ai coding plan instead of the Anthropic plan. The calling session keeps
the judgment work: it writes the brief and reviews the result. GLM's own
report is never verification.

Subcommands (each prints exactly ONE compact JSON line on stdout; progress
and the heartbeat go to stderr, same contract as the engine):

  run     --brief <file> [--repo <path>]... [options]   start a task
  resume  <run-id> [--prompt <text>]                     continue the session
  status  <run-id>                                      report run state
  apply   <run-id> [--force]                            land the diff
  clean   <run-id> [--force]                            remove worktree+branch

Isolation (plan 2a): every run edits a dedicated `git worktree` per --repo
(<repo>-glm-<run-id>, branch glm/<run-id>, based at the repo's HEAD). The
caller's tree is never edited; `apply` is the explicit integration step.

Rules (plan 2b): GLM is told (fixed preamble) not to commit/push/stash/
reset/rebase/merge/cherry-pick, switch or create branches, edit outside the
worktree, or read .env files or keys; and a best-effort `git` shim first on
PATH refuses those forms outright. An absolute-path git call bypasses the
shim - the fixed warning says so.

Model and effort (plan 2c): the headless CLI has no model/effort flag.
ZCode 0.16.9 resolves the model from `model.main` in ~/.zcode/cli/config.json
and the effort from `config.defaultModelSelection.options.reasoningLevel` in
~/.zcode/v2/provider_config.json (verified against the CLI bundle - see the
plan's "As built"; the desktop app's
provider.<p>.models.<m>.reasoning.defaultVariant in ~/.zcode/v2/config.json
is written too, matching the plan's named key). No ZCODE_* env var can
redirect ~/.zcode/cli/config.json, so per-run private copies are impossible
and the runner edits the shared files under
~/.countersign/zcode-config.lock, saving originals + written values next to
the lock and restoring them in a finally block and on SIGINT/SIGTERM. A key
another writer changed mid-run is left alone with a loud warning.

Usage limits (plan 2d): before starting, GET the z.ai usage monitor (key
from ZAI_API_KEY or delegate.zai_key_file in ~/.countersign/config.json; the
key is never logged). At/over delegate.max_start_share.five_hour (default
0.6) or .weekly (default 0.9) the run is refused with both reset times. A
limit hit mid-run: --on-limit return (default) stops with outcome
rate-limited (resume later); wait sleeps to the real nextResetTime + 3min
and resumes the session, at most --max-waits times.

Exit codes: 0 done (apply/clean success) | 2 everything else
(rate-limited / failed / timeout / refused / interrupted / locked).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from zcode_common import (  # noqa: E402
    OrchestratorError,
    _loads_cli_json,
    _looks_rate_limited,
    _run,
    _zcode_failure_hint,
    _zcode_provider_env,
    log,
    resolve_zcode_cmd,
    set_heartbeat_secs,
    zcode_api_key,
    zcode_model_desc,
    zcode_usage_entry,
)

EXIT_DONE = 0
EXIT_ERR = 2

QUOTA_ENDPOINT_DEFAULT = "https://api.z.ai/api/monitor/usage/quota/limit"
DEFAULT_TIMEOUT_SECS = 900
DEFAULT_EFFORT = "high"
DEFAULT_MAX_START_SHARE = {"five_hour": 0.6, "weekly": 0.9}
WAIT_GRACE_SECS = 180            # sleep to nextResetTime + 3 minutes
CHECK_TAIL_LINES = 40
SHIM_WARNING = ("git shim is best-effort: an absolute-path git call "
                "(e.g. /usr/bin/git) bypasses it")

DELEGATE_PREAMBLE = """COUNTERSIGN DELEGATE RUN - HARD RULES (obey exactly):
- Do NOT commit, push, stash, reset, rebase, merge or cherry-pick.
- Do NOT switch, create or delete branches, and do NOT create or remove git worktrees.
- Edit ONLY files inside the provided worktree(s) (your working directory).
- Do NOT read or print .env files, API keys or credentials files.
- A `git` shim on PATH refuses history-changing commands; staying inside the
  rules above is on you.
"""

WAIT_RESUME_PROMPT = ("COUNTERSIGN DELEGATE RUN (continued). You were "
                      "interrupted by a usage limit; the window has reset. "
                      "Check the current state of the tree and finish the "
                      "task from the original brief.")

# run id of the run currently executing in this process (error reporting)
_CURRENT_RUN_ID = ""


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_plugin_config() -> dict:
    try:
        data = json.loads((Path.home() / ".countersign" / "config.json")
                          .read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_json_file(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".dl-tmp")
    # The replaced file keeps the original's permissions: ZCode's
    # provider_config.json is owner-only (0600), and a fresh temp file would
    # come up at the umask default and widen it.
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o600
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def git(repo: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *argv], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=120)
    if check and proc.returncode != 0:
        raise OrchestratorError(
            f"git -C {repo} {' '.join(argv[:3])} failed: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}")
    return proc


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def parse_scoped_cmd(specs: list, repo_names: list) -> list:
    """['<repo-name>:<cmd>' | '<cmd>', ...] -> [(repo_name or None, cmd)].
    An unprefixed command runs in EVERY worktree; a prefixed one only in the
    named repo's worktree."""
    out = []
    for raw in specs:
        s = raw.strip()
        if not s:
            continue
        head, sep, rest = s.partition(":")
        if sep and head in repo_names and rest.strip():
            out.append((head, rest.strip()))
        else:
            out.append((None, s))
    return out


# --------------------------------------------------------------------------
# The git shim (plan 2b)
# --------------------------------------------------------------------------

GIT_SHIM = r'''#!/usr/bin/env python3
"""countersign delegate git shim: refuses history/branch-changing git forms.

Best-effort by design: an absolute-path git call bypasses it entirely.
Everything not refused is passed to the real git, found by searching PATH
with this shim's own directory left out.
"""
import os
import subprocess
import sys

REFUSED_SUBCOMMANDS = {
    "commit", "push", "stash", "rebase", "merge", "cherry-pick", "am",
    "tag", "worktree", "switch",
}

# global options that consume the NEXT argument
_VALUE_OPTS = {"-C", "--git-dir", "--work-tree", "--namespace", "-c",
               "--exec-path", "--super-prefix", "--pathspec-from-file"}


def refuse(reason):
    print(f"refused by countersign delegate: {reason}", file=sys.stderr)
    sys.exit(1)


def find_real_git():
    own = os.path.dirname(os.path.abspath(__file__))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        d = os.path.abspath(entry)
        if d == own:
            continue
        for name in ("git", "git.exe"):
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return "git"


def is_revision(arg):
    p = subprocess.run([find_real_git(), "rev-parse", "--verify", "--quiet",
                        arg + "^{commit}"], capture_output=True)
    return p.returncode == 0


def subcommand_and_rest(argv):
    """(subcommand, rest) skipping global options that take a value."""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            return None, argv[i:]
        if a in _VALUE_OPTS:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a, argv[i + 1:]
    return None, argv[i:]


def checkout_refused(rest):
    """checkout forms that change branch or HEAD are refused (plan 2b)."""
    args = []
    for a in rest:
        if a == "--":
            return False        # checkout -- <paths>: pure path restore
        if a in ("-b", "-B", "--orphan", "-"):
            return True         # "-" is the previously checked-out branch
        if a.startswith("-"):
            continue
        args.append(a)
    if not args:
        return False
    first = args[0]
    if os.path.exists(first):
        return False            # checkout <existing path>
    if is_revision(first):
        return True             # checkout <rev>: moves HEAD/branch
    return False                # a path that does not exist: git errors itself


def reset_refused(rest):
    """reset with --hard/--soft/--merge/--keep or a revision before --."""
    for a in rest:
        if a == "--":
            return False
        if a in ("--hard", "--soft", "--merge", "--keep"):
            return True
        if a.startswith("-"):
            continue
        # first non-option argument: a revision (not a pathspec) refuses
        return is_revision(a) and not os.path.exists(a)
    return False                # bare reset / reset [--] <paths>: unstaging


def branch_refused(rest):
    """branch forms that create, delete, rename, copy or force-move."""
    read_only = {"--list", "-l", "-a", "--all", "-v", "-vv", "--verbose",
                 "--show-current", "--contains", "--no-contains", "--merged",
                 "--no-merged", "-t", "--sort", "--format"}
    saw_read_only_flag = False
    for a in rest:
        if a == "--":
            break
        if a in ("-d", "-D", "-m", "-M", "-c", "-C", "-f", "--force",
                 "--delete", "--move", "--copy", "--edit-description"):
            return True
        if a in read_only:
            saw_read_only_flag = True
            continue
        if a.startswith("-"):
            continue
        if not saw_read_only_flag:
            return True         # a bare branch name would create it
    return False


def main():
    argv = sys.argv[1:]
    sub, rest = subcommand_and_rest(argv)
    if sub is None:
        os.execv(find_real_git(), ["git", *argv])
    if sub in REFUSED_SUBCOMMANDS:
        refuse(f"'git {sub}' changes history/refs and is not allowed "
               "in a delegated run")
    if sub == "checkout" and checkout_refused(rest):
        refuse("'git checkout' may not change branch or HEAD in a delegated run")
    if sub == "reset" and reset_refused(rest):
        refuse("'git reset' may not move HEAD or discard the index here")
    if sub == "branch" and branch_refused(rest):
        refuse("'git branch' may not create, delete, rename or copy branches here")
    os.execv(find_real_git(), ["git", *argv])


if __name__ == "__main__":
    main()
'''


def write_git_shim(shim_dir: Path) -> Path:
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / "git"
    shim.write_text(GIT_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    if os.name == "nt":
        # best-effort Windows wrapper (PATHEXT resolves .cmd, not bare files)
        (shim_dir / "git.cmd").write_text(
            '@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, shim),
            encoding="utf-8")
    return shim_dir


# --------------------------------------------------------------------------
# Model / effort configuration under a lock (plan 2c)
# --------------------------------------------------------------------------

def _cli_config_path() -> Path:
    return Path.home() / ".zcode" / "cli" / "config.json"


def _personal_catalog_path() -> Path:
    return Path.home() / ".zcode" / "v2" / "provider_config.json"


def _desktop_config_path() -> Path:
    return Path.home() / ".zcode" / "v2" / "config.json"


def _get_path(obj, key):
    for k in key:
        if not isinstance(obj, dict) or k not in obj:
            return None, False
        obj = obj[k]
    return obj, True


def _set_path(obj, key, value) -> None:
    for k in key[:-1]:
        nxt = obj.get(k)
        if not isinstance(nxt, dict):
            nxt = obj[k] = {}
        obj = nxt
    obj[key[-1]] = value


def _del_path(obj, key) -> None:
    for k in key[:-1]:
        if not isinstance(obj, dict) or k not in obj:
            return
        obj = obj[k]
    if isinstance(obj, dict):
        obj.pop(key[-1], None)


def _current_pin() -> Optional[str]:
    m = read_json_file(_cli_config_path(), {}).get("model") \
        if isinstance(read_json_file(_cli_config_path(), {}), dict) else None
    if isinstance(m, dict) and m.get("main"):
        return str(m["main"])
    if isinstance(m, str) and m:
        return m
    return None


def _split_pin(pin: Optional[str]) -> tuple:
    if pin and "/" in pin:
        p, m = pin.split("/", 1)
        return p, m
    return "zai-coding-plan", "GLM-5.3"


def catalog_variants(model: str) -> Optional[list]:
    """Effort variants the catalog lists for the model, when a catalog is
    readable. Preferred source: the desktop v2 config's reasoning.variants;
    fallback: the app-shipped builtin catalog's modelRules optionSpecs."""
    v2 = read_json_file(_desktop_config_path(), {})
    if isinstance(v2, dict):
        for prov in (v2.get("provider") or {}).values():
            if not isinstance(prov, dict):
                continue
            ment = (prov.get("models") or {}).get(model)
            if isinstance(ment, dict):
                variants = (ment.get("reasoning") or {}).get("variants")
                if isinstance(variants, list) and variants:
                    return [str(v) for v in variants]
    for cjs in ("/opt/ZCode/resources/glm/zcode.cjs",
                "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"):
        catalog = (Path(cjs).resolve().parent.parent / "config" / "provider"
                   / "zcode-builtin.json")
        if not catalog.is_file():
            continue
        rules = (read_json_file(catalog, {}).get("config", {})
                 .get("modelConfigRules", {}).get("modelRules", []))
        # the most SPECIFIC matching rule wins: the glm-5.3 pattern must beat
        # the generic glm-5 and ".*" fallback rules ordered before it
        best = None
        for rule in rules if isinstance(rules, list) else []:
            if not isinstance(rule, dict):
                continue
            mm = str(rule.get("modelMatch", ""))
            try:
                rx = re.compile(mm, re.IGNORECASE)
            except re.error:
                continue
            if not (rx.fullmatch(model) or rx.search(model)):
                continue
            values = (rule.get("config", {}).get("optionSpecs", {})
                      .get("reasoningLevel", {}).get("values"))
            if isinstance(values, list) and values and (
                    best is None or len(mm) > best[0]):
                best = (len(mm), [str(v) for v in values])
        if best:
            return best[1]
    return None


def validate_effort(effort: str, model: str) -> Optional[str]:
    """None when OK; an error message when the catalog does not list effort."""
    variants = catalog_variants(model)
    if variants is None:
        return None            # no readable catalog: cannot validate offline
    if effort in variants:
        return None
    return (f"reasoning effort '{effort}' is not listed for {model} in the "
            f"provider catalog (variants: {', '.join(variants)})")


def _lock_path() -> Path:
    return Path.home() / ".countersign" / "zcode-config.lock"


def _state_path() -> Path:
    return Path.home() / ".countersign" / "zcode-config.state.json"


def acquire_config_lock(warnings: list) -> Optional[Path]:
    """Exclusive lock around the shared zcode config files. A lock whose
    holder PID is dead is taken over, and the dead run's saved originals are
    restored first (restore_config_state)."""
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"pid={os.getpid()} started={now_iso()}")
            return lock
        except FileExistsError:
            text = ""
            try:
                text = lock.read_text(encoding="utf-8")
            except OSError:
                pass
            m = re.search(r"^pid=(\d+)\b", text, re.M)
            holder = int(m.group(1)) if m else None
            if holder is not None and not _pid_alive(holder):
                log(f"zcode config lock holder pid {holder} is dead - taking "
                    "over and restoring its saved originals first")
                prior = read_json_file(_state_path(), None)
                if isinstance(prior, dict):
                    warnings.extend(restore_config_state(prior))
                lock.unlink(missing_ok=True)
                continue
            return None


def restore_config_state(state: dict) -> list:
    """Restore every saved key whose current value still equals what the run
    wrote. A key another writer changed mid-run is NOT overwritten: a loud
    warning (returned, and logged) names the key, the found value and the
    original that was skipped."""
    warnings = []
    for entry in state.get("entries", []):
        try:
            path = Path(str(entry["file"]))
            key = list(entry["key"])
        except (KeyError, TypeError):
            continue
        data = read_json_file(path, None)
        if not isinstance(data, dict):
            continue
        current, _ = _get_path(data, key)
        if current == entry.get("written"):
            if entry.get("existed"):
                _set_path(data, key, entry.get("original"))
            else:
                _del_path(data, key)
            write_json_atomic(path, data)
            log(f"restored {path.name}:{'.'.join(key)}")
        elif current != entry.get("original"):
            w = (f"zcode config key {path.name}:{'.'.join(key)} was changed "
                 f"by another writer during the run - found "
                 f"{json.dumps(current)}, left it in place (the pre-run value "
                 f"{json.dumps(entry.get('original'))} was NOT restored)")
            warnings.append(w)
            log("WARNING: " + w)
    return warnings


def apply_model_effort(model: Optional[str], effort: str,
                       warnings: list) -> list:
    """Write the model pin + effort keys (caller holds the lock). Returns the
    edit records (also persisted next to the lock for stale-lock takeover)."""
    entries = []

    def edit(path: Path, key: list, value):
        data = read_json_file(path, None)
        if not isinstance(data, dict):
            data = {}
        current, existed = _get_path(data, key)
        entries.append({"file": str(path), "key": key, "existed": existed,
                        "original": current if existed else None,
                        "written": value})
        _set_path(data, key, value)
        write_json_atomic(path, data)

    pin = _current_pin()
    pin_provider, pin_model = _split_pin(pin)

    personal = read_json_file(_personal_catalog_path(), {})
    selection = {}
    if isinstance(personal, dict):
        sel = (personal.get("config") or {}).get("defaultModelSelection")
        if isinstance(sel, dict):
            selection = sel
    provider_id = selection.get("providerId") or pin_provider
    target_model = model or selection.get("modelId") or pin_model

    if model:
        edit(_cli_config_path(), ["model", "main"], f"{pin_provider}/{model}")
    # the key the headless CLI actually reads for effort (see "As built")
    edit(_personal_catalog_path(), ["config", "defaultModelSelection"],
         {"providerId": provider_id, "modelId": target_model,
          "options": {"reasoningLevel": effort}})
    # the desktop app's key the plan names (kept consistent for the TUI)
    desktop = read_json_file(_desktop_config_path(), {})
    if isinstance(desktop, dict):
        providers = desktop.get("provider") or {}
        v2_provider = next(
            (p for p in (f"builtin:{provider_id}", provider_id)
             if isinstance(providers.get(p), dict)
             and isinstance((providers[p].get("models") or {})
                            .get(target_model), dict)),
            None)
        if v2_provider is None:
            v2_provider = next(
                (p for p, pv in providers.items()
                 if isinstance(pv, dict)
                 and isinstance((pv.get("models") or {}).get(target_model), dict)),
                None)
        if v2_provider:
            edit(_desktop_config_path(),
                 ["provider", v2_provider, "models", target_model,
                  "reasoning", "defaultVariant"], effort)
        else:
            warnings.append(
                f"no provider in ~/.zcode/v2/config.json lists model "
                f"{target_model} - the desktop catalog defaultVariant was "
                "not written (the CLI-served effort key was)")
    write_json_atomic(_state_path(), {"entries": entries, "written_at": now_iso()})
    return entries


# --------------------------------------------------------------------------
# Usage monitor (plan 2d)
# --------------------------------------------------------------------------

def quota_key(config: dict) -> Optional[str]:
    """ZAI_API_KEY env var wins; else a key file named by delegate.zai_key_file.
    The desktop config key is deliberately NOT used: the monitor endpoint
    refused it (verified 2026-10-22). The key is never logged or printed."""
    env_key = os.environ.get("ZAI_API_KEY")
    if env_key:
        return env_key
    key_file = (config.get("delegate") or {}).get("zai_key_file")
    if key_file:
        try:
            text = Path(str(key_file)).expanduser().read_text(
                encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return None


def fetch_quota(endpoint: str, key: Optional[str]) -> Optional[dict]:
    """GET the usage monitor. Returns {'five_hour': {...}, 'weekly': {...}}
    with share (0..1) and next_reset_ms, or None when unavailable. file://
    endpoints are honored so tests can replay recorded responses."""
    url = endpoint if "://" in endpoint else f"file://{endpoint}"
    try:
        if url.startswith("file://"):
            with urlopen(url, timeout=30) as r:
                body = r.read()
        else:
            req = Request(url, headers={"Authorization": f"Bearer {key}"})
            with urlopen(req, timeout=30) as r:
                body = r.read()
        data = json.loads(body.decode("utf-8"))
    except (URLError, OSError, ValueError) as e:
        log(f"quota monitor unavailable: {e}")
        return None
    out = {}
    for lim in ((data.get("data") or {}).get("limits") or []):
        if not isinstance(lim, dict) or lim.get("type") != "TOKENS_LIMIT":
            continue
        unit, number = lim.get("unit"), lim.get("number")
        slot = ("five_hour" if (unit, number) == (3, 5)
                else "weekly" if (unit, number) == (6, 1) else None)
        if slot and slot not in out:
            out[slot] = {"share": float(lim.get("percentage", 0)) / 100.0,
                         "next_reset_ms": lim.get("nextResetTime")}
    return out or None


def quota_resets(quota: Optional[dict]) -> dict:
    resets = {}
    if quota:
        for slot in ("five_hour", "weekly"):
            resets[slot] = (quota.get(slot) or {}).get("next_reset_ms")
    return resets


# --------------------------------------------------------------------------
# Run state + report
# --------------------------------------------------------------------------

@dataclass
class DelegateReport:
    outcome: str = "error"
    run_id: str = ""
    session_id: Optional[str] = None
    repos: list = field(default_factory=list)
    reply: str = ""
    checks: list = field(default_factory=list)
    model_served: Optional[str] = None
    quota: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    error: Optional[str] = None
    duration_s: float = 0.0
    waits_used: int = 0
    wait_seconds_total: float = 0.0


def delegate_dir() -> Path:
    return Path.home() / ".countersign" / "delegate"


def run_dir(run_id: str) -> Path:
    return delegate_dir() / run_id


# --------------------------------------------------------------------------
# Diffs, files, apply, clean (plan 2g)
# --------------------------------------------------------------------------

def intent_to_add(worktree: Path) -> None:
    git(worktree, "add", "-A", "-N")


def worktree_diff(worktree: Path, base: str) -> str:
    return git(worktree, "diff", "--binary", base).stdout


def worktree_files(worktree: Path, base: str) -> list:
    """base..working-tree file list: committed, staged, unstaged and new
    (via intent-to-add). Renames are decomposed into add+delete."""
    intent_to_add(worktree)
    files = []
    numstat = git(worktree, "diff", "--numstat", "--no-renames", base).stdout
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, deleted_s, path = parts
        in_base = git(worktree, "cat-file", "-e", f"{base}:{path}",
                      check=False).returncode == 0
        exists_now = (worktree / path).exists()
        status = "A" if not in_base else ("D" if not exists_now else "M")
        files.append({"path": path,
                      "added": int(added_s) if added_s.isdigit() else 0,
                      "deleted": int(deleted_s) if deleted_s.isdigit() else 0,
                      "status": status})
    return files


def created_ignored_files(worktree: Path, started: float) -> list:
    out = []
    proc = git(worktree, "status", "--porcelain", "--ignored")
    for line in proc.stdout.splitlines():
        if not line.startswith("!!"):
            continue
        path = line[3:].strip().strip('"')
        try:
            if (worktree / path).stat().st_mtime >= started - 1:
                out.append(path)
        except OSError:
            continue
        if len(out) >= 20:
            break
    return out


def diff_paths(diff: str) -> list:
    paths = []
    for line in diff.splitlines():
        if not line.startswith("diff --git "):
            continue
        m = re.match(r'diff --git a/(.*?) b/(.*)$', line)
        if m:
            p = m.group(1).strip('"')
            if p and p not in paths:
                paths.append(p)
    return paths


def caller_dirty_paths(repo: Path) -> set:
    out = set()
    for line in git(repo, "status", "--porcelain").stdout.splitlines():
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        out.add(entry.strip('"'))
    return out


# --------------------------------------------------------------------------
# The runner
# --------------------------------------------------------------------------

class DelegateRun:
    def __init__(self, run_id: str, args, config: dict, warnings: list):
        self.run_id = run_id
        self.args = args
        self.config = config
        self.report = DelegateReport(run_id=run_id)
        self.report.warnings = warnings     # shared list, by reference
        self.rdir = run_dir(run_id)
        self.repos: list[dict] = []
        self.session_id: Optional[str] = None
        self.state: dict = {}
        self.lock: Optional[Path] = None
        self.config_entries: list = []
        self.call_no = 0

    # -- state ------------------------------------------------------------

    def load_state(self) -> bool:
        self.state = read_json_file(self.rdir / "state.json", None)
        if not isinstance(self.state, dict):
            return False
        self.repos = [r for r in self.state.get("repos", []) if isinstance(r, dict)]
        self.session_id = self.state.get("session_id")
        self.call_no = int(self.state.get("call_no", 0))
        return True

    def save_state(self) -> None:
        self.state.update({
            "run_id": self.run_id,
            "repos": self.repos,
            "session_id": self.session_id,
            "call_no": self.call_no,
            "updated": now_iso(),
        })
        write_json_atomic(self.rdir / "state.json", self.state)

    # -- setup ------------------------------------------------------------

    def create_worktrees(self, repo_paths: list) -> None:
        caller_before = self.state.setdefault("caller_status_before", {})
        for repo in repo_paths:
            repo = repo.resolve()
            base = git(repo, "rev-parse", "HEAD").stdout.strip()
            worktree = Path(str(repo) + f"-glm-{self.run_id}")
            git(repo, "worktree", "add", str(worktree),
                "-b", f"glm/{self.run_id}", base)
            self.repos.append({"repo": str(repo), "worktree": str(worktree),
                               "branch": f"glm/{self.run_id}", "base": base})
            dirty = sorted(caller_dirty_paths(repo))
            caller_before[str(repo)] = dirty
            if dirty:
                self.report.warnings.append(
                    f"{repo.name} has {len(dirty)} uncommitted caller "
                    "change(s) that are NOT in the delegate worktree "
                    "(based at HEAD)")
                log(f"WARNING: {repo.name}: {len(dirty)} uncommitted caller "
                    "change(s) not in the worktree")

    def glm_cwd(self) -> Path:
        if len(self.repos) == 1:
            return Path(self.repos[0]["worktree"])
        ws = self.rdir / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        for r in self.repos:
            link = ws / Path(r["worktree"]).name
            if link.is_symlink() or link.exists():
                continue
            try:
                os.symlink(r["worktree"], link)
            except OSError as e:
                raise OrchestratorError(f"could not link {link}: {e}")
        return ws

    # -- agent ------------------------------------------------------------

    def zcode_cmd(self) -> list:
        return resolve_zcode_cmd(getattr(self.args, "zcode_cli", None),
                                 dry_run=False)

    def mode(self) -> str:
        return getattr(self.args, "mode", None) or self.state.get("mode") \
            or "yolo"

    def call_glm(self, prompt: str, resume: bool) -> dict:
        """One headless ZCode call. Returns {'kind': ok|limit|fail|timeout}."""
        self.call_no += 1
        cmd = self.zcode_cmd() + [
            "--prompt", prompt, "--json", "--mode", self.mode(),
            "--no-color", "--attach", str(self.rdir / "brief.md"),
        ]
        sid = self.session_id if resume else None
        if sid:
            cmd += ["--resume", sid]
        env_extra = _zcode_provider_env(
            SimpleNamespace(zcode_cmd=self.zcode_cmd()))
        api_key = zcode_api_key()
        if api_key:
            env_extra["ZCODE_API_KEY"] = api_key
        shim = self.rdir / "shim"
        env_extra["PATH"] = str(shim) + os.pathsep + os.environ.get("PATH", "")
        log(f"zcode call {self.call_no} (resume={bool(sid)}) starting")
        result = {"kind": "fail", "stderr": ""}
        try:
            proc = _run(cmd, timeout=self.args.timeout, cwd=self.glm_cwd(),
                        env_extra=env_extra)
        except OrchestratorError as e:
            result["stderr"] = str(e)
            if "timed out" in str(e):
                result["kind"] = "timeout"
            self._log_io(err=str(e))
            return result
        self._log_io(out=proc.stdout, err_text=proc.stderr)
        if proc.returncode != 0:
            hint = _zcode_failure_hint(proc.stderr.strip())
            result["stderr"] = hint
            if _looks_rate_limited(hint) or "Usage limit reached for 5 hour" in hint:
                result["kind"] = "limit"
            return result
        data = _loads_cli_json(proc.stdout)
        if data is None:
            result["stderr"] = "zcode stdout was not JSON: " + proc.stdout[:400]
            return result
        usage = zcode_usage_entry(data)
        text = str(data.get("response", ""))
        self.session_id = data.get("sessionId") or self.session_id
        result.update({"kind": "ok", "text": text, "usage": usage})
        # The CLI surfaces a spent coding-plan window as the turn's final
        # message (session id still known) - detect that shape too.
        if "Usage limit reached for 5 hour" in text or _looks_rate_limited(text):
            result["kind"] = "limit"
        return result

    def _log_io(self, err: Optional[str] = None, out: str = "",
                err_text: str = "") -> None:
        with open(self.rdir / "zcode-io.log", "a", encoding="utf-8") as f:
            f.write(f"\n=== call {self.call_no} @ {now_iso()} ===\n")
            if out:
                f.write("--- stdout ---\n" + out + "\n")
            if err_text:
                f.write("--- stderr ---\n" + err_text + "\n")
            if err:
                f.write("--- error ---\n" + err + "\n")

    # -- scoped setup/check commands ---------------------------------------

    def run_scoped_cmds(self, specs: list, phase: str) -> None:
        names = [Path(r["repo"]).name for r in self.repos]
        shell = ["cmd", "/c"] if os.name == "nt" else ["/bin/sh", "-c"]
        for name, cmd_s in parse_scoped_cmd(specs, names):
            targets = ([r for r in self.repos if Path(r["repo"]).name == name]
                       if name else self.repos)
            if not targets:
                self.report.warnings.append(
                    f"{phase} command scoped to unknown repo '{name}' - skipped")
                continue
            for r in targets:
                log(f"{phase} in {Path(r['repo']).name}: {cmd_s}")
                try:
                    proc = subprocess.run(
                        shell + [cmd_s], cwd=str(Path(r["worktree"])),
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=self.args.timeout)
                    exit_code, out = proc.returncode, (proc.stdout + proc.stderr)
                except subprocess.TimeoutExpired:
                    exit_code, out = -1, f"(timed out after {self.args.timeout}s)"
                except OSError as e:
                    exit_code, out = -1, str(e)
                tail = "\n".join(out.splitlines()[-CHECK_TAIL_LINES:])
                self.report.checks.append(
                    {"cmd": cmd_s, "repo": Path(r["repo"]).name,
                     "exit": exit_code, "tail": tail})
                log(f"{phase} '{cmd_s}' in {Path(r['repo']).name} -> exit {exit_code}")

    # -- usage accounting ----------------------------------------------------

    def accumulate_usage(self, usage: dict) -> None:
        self.report.usage = self.report.usage or {}
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                self.report.usage[k] = self.report.usage.get(k, 0) + v

    def finalize_usage(self) -> None:
        def pick(*keys):
            for k in keys:
                if isinstance(self.report.usage.get(k), (int, float)):
                    return self.report.usage[k]
            return None
        self.report.usage = {
            "input": pick("input", "input_tokens", "inputTokens"),
            "output": pick("output", "output_tokens", "outputTokens"),
            "cache_read": pick("cache_read", "cache_read_tokens",
                               "cache_read_input_tokens", "cacheReadTokens"),
        }

    # -- post-run accounting -------------------------------------------------

    def record_repos(self, started: float) -> None:
        for r in self.repos:
            wt = Path(r["worktree"])
            self.report.repos.append({
                "repo": r["repo"], "worktree": r["worktree"],
                "branch": r["branch"], "base": r["base"],
                "files": worktree_files(wt, r["base"]),
                "glm_committed": git(wt, "rev-parse", "HEAD").stdout.strip()
                != r["base"],
            })
            for ign in created_ignored_files(wt, started):
                self.report.warnings.append(
                    "gitignored file created during the run (not in the "
                    f"diff): {Path(r['repo']).name}/{ign}")


class _Finish(Exception):
    """Internal: stop the locked section with an exit code (restore still runs)."""
    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def finish(run: DelegateRun, started: float, code: int) -> int:
    run.report.duration_s = round(time.time() - started, 1)
    write_json_atomic(run.rdir / "report.json", asdict(run.report))
    print(json.dumps(asdict(run.report), separators=(",", ":")), flush=True)
    return code


# --------------------------------------------------------------------------
# run / resume
# --------------------------------------------------------------------------

def run_command(args, config: dict) -> int:
    global _CURRENT_RUN_ID
    warnings: list = [SHIM_WARNING]
    run_id = (f"glm-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-"
              f"{os.urandom(3).hex()}")
    _CURRENT_RUN_ID = run_id
    run = DelegateRun(run_id, args, config, warnings)
    run.rdir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    brief_path = Path(args.brief)
    if not brief_path.is_file():
        run.report.outcome = "refused"
        run.report.error = f"--brief {brief_path} not found"
        return finish(run, started, EXIT_ERR)
    shutil.copyfile(brief_path, run.rdir / "brief.md")
    write_git_shim(run.rdir / "shim")

    # resolve repos
    if args.repo:
        repo_paths = [Path(r).expanduser() for r in args.repo]
    else:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            run.report.outcome = "refused"
            run.report.error = ("no --repo given and the current directory "
                                "is not inside a git repository")
            return finish(run, started, EXIT_ERR)
        repo_paths = [Path(proc.stdout.strip())]
    for rp in repo_paths:
        if not rp.is_dir() or git(rp, "rev-parse", "--git-dir",
                                  check=False).returncode != 0:
            run.report.outcome = "refused"
            run.report.error = f"--repo {rp} is not a git repository"
            return finish(run, started, EXIT_ERR)

    # model/effort resolution + validation
    dconf = config.get("delegate") or {}
    effort = args.effort or dconf.get("effort") or DEFAULT_EFFORT
    model = args.model or dconf.get("model")
    verr = validate_effort(effort, model or _split_pin(_current_pin())[1])
    if verr:
        run.report.outcome = "refused"
        run.report.error = verr
        return finish(run, started, EXIT_ERR)

    # usage pre-check (plan 2d)
    key = quota_key(config)
    if key:
        quota_before = fetch_quota(args.quota_endpoint, key)
        run.report.quota["before"] = quota_before
        if quota_before:
            fh = quota_before.get("five_hour", {}).get("share", 0.0)
            wk = quota_before.get("weekly", {}).get("share", 0.0)
            limits = dconf.get("max_start_share") or {}
            max_fh = limits.get("five_hour", DEFAULT_MAX_START_SHARE["five_hour"])
            max_wk = limits.get("weekly", DEFAULT_MAX_START_SHARE["weekly"])
            if fh >= max_fh or wk >= max_wk:
                run.report.outcome = "rate-limited"
                run.report.quota["resets"] = quota_resets(quota_before)
                run.report.error = (
                    f"quota pre-check refused: 5h window at {fh:.0%} (max "
                    f"start {max_fh:.0%}), weekly at {wk:.0%} (max start "
                    f"{max_wk:.0%}); resets: "
                    + ", ".join(
                        f"{name} {time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(ms / 1000))}"
                        for name, ms in quota_resets(quota_before).items() if ms))
                log("REFUSED: " + run.report.error)
                return finish(run, started, EXIT_ERR)
    else:
        warnings.append("quota pre-check skipped: no ZAI_API_KEY and no "
                        "delegate.zai_key_file - limits will be caught from "
                        "the CLI's error")

    # config lock + model/effort application, restored on every exit path
    run.lock = acquire_config_lock(warnings)
    if run.lock is None:
        run.report.outcome = "locked"
        run.report.error = ("another process holds "
                            "~/.countersign/zcode-config.lock and its PID is "
                            "alive; delegate runs are serialized on the "
                            "shared zcode config")
        return finish(run, started, EXIT_ERR)
    code = EXIT_DONE
    try:
        run.config_entries = apply_model_effort(model, effort, warnings)
        run.state.update({"effort": effort, "model": model,
                          "mode": args.mode, "on_limit": args.on_limit})
        run.create_worktrees(repo_paths)
        run.save_state()

        run.run_scoped_cmds(args.setup or [], "setup")
        failed_setup = [c for c in run.report.checks if c["exit"] != 0]
        if failed_setup:
            run.report.outcome = "failed"
            run.report.error = (
                f"setup command failed: {failed_setup[0]['cmd']} (exit "
                f"{failed_setup[0]['exit']}); GLM was not run - fix the "
                "environment and re-run")
            raise _Finish(EXIT_ERR)
        run.report.checks = []

        prompt = DELEGATE_PREAMBLE + (
            "\nThe task brief is attached. Implement it now in the provided "
            "worktree(s). Reply with a short summary of what you did, files "
            "touched, checks you ran, and anything you decided that the "
            "brief did not say.")
        (run.rdir / "prompt.txt").write_text(prompt, encoding="utf-8")

        waits_used = 0
        while True:
            res = run.call_glm(prompt, resume=(run.call_no > 0))
            run.save_state()
            if res["kind"] == "ok":
                run.report.reply = res["text"][:4000]
                run.accumulate_usage(res.get("usage", {}))
                break
            if res["kind"] == "timeout":
                run.report.outcome = "timeout"
                run.report.error = f"zcode call timed out: {res['stderr'][:300]}"
                raise _Finish(EXIT_ERR)
            if res["kind"] != "limit":
                run.report.outcome = "failed"
                run.report.error = f"zcode call failed: {res['stderr'][:300]}"
                raise _Finish(EXIT_ERR)
            # usage limit mid-run
            run.report.session_id = run.session_id
            if args.on_limit == "return" or waits_used >= args.max_waits:
                run.report.outcome = "rate-limited"
                if key:
                    run.report.quota["resets"] = quota_resets(
                        fetch_quota(args.quota_endpoint, key))
                run.report.error = (
                    "z.ai usage limit hit mid-run"
                    + (f" (after {waits_used} wait(s))" if waits_used else "")
                    + f"; session {run.session_id} - re-run 'resume {run_id}' "
                    "after the window resets. Resets: "
                    + json.dumps(run.report.quota.get("resets", {})))
                log("RATE-LIMITED: " + run.report.error)
                raise _Finish(EXIT_ERR)
            # --on-limit wait: sleep to the REAL reset time, then continue
            q = fetch_quota(args.quota_endpoint, key) if key else None
            reset_ms = (q or {}).get("five_hour", {}).get("next_reset_ms")
            if not reset_ms:
                run.report.outcome = "rate-limited"
                run.report.error = ("usage limit hit and --on-limit wait "
                                    "cannot compute the reset time (no quota "
                                    f"monitor) - re-run 'resume {run_id}' later")
                raise _Finish(EXIT_ERR)
            wait_s = max(0.0, reset_ms / 1000.0 + WAIT_GRACE_SECS - time.time())
            waits_used += 1
            run.report.waits_used = waits_used
            run.report.wait_seconds_total = round(
                run.report.wait_seconds_total + wait_s, 1)
            scale = os.environ.get("CS_DELEGATE_WAIT_SCALE")
            scaled = wait_s * float(scale) if scale not in (None, "") else wait_s
            log(f"usage limit: waiting {wait_s:.0f}s until the 5h window "
                f"resets (sleep scaled to {scaled:.1f}s, wait "
                f"{waits_used}/{args.max_waits})")
            time.sleep(max(0.0, scaled))
            prompt = WAIT_RESUME_PROMPT

        # post-run accounting
        run.report.outcome = "done"
        run.report.session_id = run.session_id
        run.report.model_served = zcode_model_desc(run.session_id)
        run.record_repos(started)
        for r in run.repos:
            now_dirty = caller_dirty_paths(Path(r["repo"]))
            before = set(run.state.get("caller_status_before", {}).get(r["repo"], []))
            for p in sorted(now_dirty - before):
                run.report.warnings.append(
                    f"caller tree changed during the run: "
                    f"{Path(r['repo']).name}/{p}")
        if key:
            run.report.quota["after"] = fetch_quota(args.quota_endpoint, key)
        run.finalize_usage()
        run.run_scoped_cmds(args.check or [], "check")
    except _Finish as stop:
        code = stop.code
    finally:
        # restore BEFORE the report line is printed, so a restore warning
        # (another writer changed a key) reaches the output contract
        warnings.extend(restore_config_state({"entries": run.config_entries}))
        run.lock.unlink(missing_ok=True)
    return finish(run, started, code)


def resume_command(args, config: dict) -> int:
    global _CURRENT_RUN_ID
    warnings: list = [SHIM_WARNING]
    run = DelegateRun(args.run_id, args, config, warnings)
    _CURRENT_RUN_ID = args.run_id
    if not run.load_state():
        run.report.outcome = "refused"
        run.report.error = f"no delegate run '{args.run_id}' under {delegate_dir()}"
        print(json.dumps(asdict(run.report), separators=(",", ":")), flush=True)
        return EXIT_ERR
    started = time.time()
    if not (run.rdir / "brief.md").is_file():
        run.report.outcome = "refused"
        run.report.error = f"run dir {run.rdir} has no brief.md"
        print(json.dumps(asdict(run.report), separators=(",", ":")), flush=True)
        return EXIT_ERR
    write_git_shim(run.rdir / "shim")
    prompt = DELEGATE_PREAMBLE + "\n" + (args.prompt or WAIT_RESUME_PROMPT)
    run.lock = acquire_config_lock(warnings)
    if run.lock is None:
        run.report.outcome = "locked"
        run.report.error = ("another process holds "
                            "~/.countersign/zcode-config.lock and its PID is alive")
        print(json.dumps(asdict(run.report), separators=(",", ":")), flush=True)
        return EXIT_ERR
    code = EXIT_DONE
    try:
        effort = run.state.get("effort") or DEFAULT_EFFORT
        model = run.state.get("model")
        run.config_entries = apply_model_effort(model, effort, warnings)
        res = run.call_glm(prompt, resume=True)
        run.save_state()
        if res["kind"] != "ok":
            run.report.outcome = ("rate-limited" if res["kind"] == "limit"
                                  else "timeout" if res["kind"] == "timeout"
                                  else "failed")
            run.report.session_id = run.session_id
            run.report.error = f"zcode call {res['kind']}: {res['stderr'][:300]}"
            raise _Finish(EXIT_ERR)
        run.report.reply = res["text"][:4000]
        run.accumulate_usage(res.get("usage", {}))
        run.report.outcome = "done"
        run.report.session_id = run.session_id
        run.report.model_served = zcode_model_desc(run.session_id)
        run.record_repos(started)
        run.finalize_usage()
        run.run_scoped_cmds(args.check or [], "check")
    except _Finish as stop:
        code = stop.code
    finally:
        warnings.extend(restore_config_state({"entries": run.config_entries}))
        run.lock.unlink(missing_ok=True)
    return finish(run, started, code)


# --------------------------------------------------------------------------
# status / apply / clean
# --------------------------------------------------------------------------

def status_command(args, config: dict) -> int:
    rdir = run_dir(args.run_id)
    state = read_json_file(rdir / "state.json", None)
    report = read_json_file(rdir / "report.json", None)
    if state is None and report is None:
        print(json.dumps({"outcome": "refused", "run_id": args.run_id,
                          "error": f"no delegate run '{args.run_id}' "
                          f"under {delegate_dir()}"},
                         separators=(",", ":")))
        return EXIT_ERR
    out = dict(report or {})
    out["run_id"] = args.run_id
    out["state"] = state or {}
    out["worktrees_present"] = [
        {"repo": r.get("repo"), "worktree": r.get("worktree"),
         "present": Path(str(r.get("worktree", "/nonexistent"))).is_dir()}
        for r in (state or {}).get("repos", [])]
    out.setdefault("outcome", "unknown")
    print(json.dumps(out, separators=(",", ":"), default=str))
    return EXIT_DONE


def apply_command(args, config: dict) -> int:
    rdir = run_dir(args.run_id)
    state = read_json_file(rdir / "state.json", None)
    if not isinstance(state, dict):
        print(json.dumps({"outcome": "refused", "run_id": args.run_id,
                          "error": f"no delegate run '{args.run_id}'",
                          "applied": [], "warnings": []},
                         separators=(",", ":")))
        return EXIT_ERR
    applied, warnings = [], []
    outcome, error = "done", None
    for r in state.get("repos", []):
        repo, wt = Path(r["repo"]), Path(r["worktree"])
        if not wt.is_dir():
            warnings.append(f"worktree {wt} is gone - skipping {repo.name}")
            continue
        intent_to_add(wt)
        patch = worktree_diff(wt, r["base"])
        if not patch.strip():
            continue
        paths = diff_paths(patch)
        overlap = sorted(set(paths) & caller_dirty_paths(repo))
        if overlap and not args.force:
            outcome, error = "refused", (
                f"{repo.name} has uncommitted caller changes in the run's "
                f"paths: {', '.join(overlap[:10])} - resolve them or pass "
                "--force")
            break
        patch_file = rdir / f"apply-{repo.name}.patch"
        patch_file.write_text(patch, encoding="utf-8")
        # Working tree only, so the caller's index (which may hold their own
        # staged work) is left alone; --3way, which stages what it applies,
        # only when a plain apply does not fit.
        proc = git(repo, "apply", str(patch_file), check=False)
        if proc.returncode != 0:
            proc = git(repo, "apply", "--3way", str(patch_file), check=False)
            if proc.returncode == 0:
                warnings.append(f"{repo.name}: the patch needed a 3-way apply, "
                                "so its changes are staged in the index")
        if proc.returncode != 0:
            outcome, error = "failed", (
                f"git apply --3way failed in {repo.name}: "
                f"{(proc.stderr or proc.stdout).strip()[:300]} - nothing "
                "committed, worktree kept")
            break
        applied.append({"repo": str(repo), "worktree": str(wt),
                        "branch": r["branch"], "base": r["base"],
                        "sha256": hashlib.sha256(patch.encode()).hexdigest(),
                        "paths": paths})
    # Recorded even when a later repo failed: the repos already applied must
    # not read as unapplied work to `clean`.
    if applied:
        existing = read_json_file(rdir / "apply.json", [])
        write_json_atomic(rdir / "apply.json",
                          (existing if isinstance(existing, list) else [])
                          + applied)
    print(json.dumps({"outcome": outcome, "run_id": args.run_id,
                      "error": error, "applied": applied, "warnings": warnings},
                     separators=(",", ":")))
    return EXIT_DONE if outcome == "done" else EXIT_ERR


def clean_command(args, config: dict) -> int:
    rdir = run_dir(args.run_id)
    state = read_json_file(rdir / "state.json", None)
    if not isinstance(state, dict):
        print(json.dumps({"outcome": "refused", "run_id": args.run_id,
                          "error": f"no delegate run '{args.run_id}'",
                          "removed": [], "warnings": []},
                         separators=(",", ":")))
        return EXIT_ERR
    apply_record = {a["repo"]: a
                    for a in read_json_file(rdir / "apply.json", [])
                    if isinstance(a, dict) and "repo" in a}
    removed, warnings = [], []
    outcome, error = "done", None
    # phase 1: decide for every repo first (no partial cleanup on refusal)
    unapplied = []
    for r in state.get("repos", []):
        repo, wt = Path(r["repo"]), Path(r["worktree"])
        if not wt.is_dir():
            continue
        intent_to_add(wt)
        patch = worktree_diff(wt, r["base"])
        head_moved = git(wt, "rev-parse", "HEAD").stdout.strip() != r["base"]
        rec = apply_record.get(str(repo))
        if rec:
            if hashlib.sha256(patch.encode()).hexdigest() != rec["sha256"]:
                unapplied = (diff_paths(patch)
                             or ["(worktree changed after apply)"])
        elif (patch.strip() or head_moved
              or git(wt, "status", "--porcelain").stdout.strip()):
            unapplied = (diff_paths(patch)
                         or git(wt, "status", "--porcelain").stdout.splitlines()[:10])
        if unapplied and not args.force:
            outcome, error = "refused", (
                f"{repo.name} worktree holds unapplied work: "
                f"{', '.join(unapplied[:10])} - run apply first, or pass "
                "--force to discard it")
            break
    # phase 2: remove
    if outcome == "done":
        for r in state.get("repos", []):
            repo, wt = Path(r["repo"]), Path(r["worktree"])
            branch = r["branch"]
            if wt.is_dir():
                git(repo, "worktree", "remove", "--force", str(wt))
            if git(repo, "rev-parse", "--verify", "--quiet",
                   f"refs/heads/{branch}", check=False).returncode == 0:
                git(repo, "branch", "-D", branch)
            removed.append({"repo": str(repo), "worktree": str(wt),
                            "branch": branch})
    print(json.dumps({"outcome": outcome, "run_id": args.run_id,
                      "error": error, "removed": removed, "warnings": warnings},
                     separators=(",", ":")))
    return EXIT_DONE if outcome == "done" else EXIT_ERR


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _signal_to_interrupt(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv: Optional[list] = None) -> int:
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _signal_to_interrupt)
    p = argparse.ArgumentParser(
        description="delegate: run one well-specified task through the "
                    "headless ZCode CLI (GLM) in an isolated worktree")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, with_checks=True):
        sp.add_argument("--zcode-cli",
                        help="explicit path to zcode.cjs (tests: a stub)")
        sp.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECS,
                        help=f"per-call timeout in seconds "
                             f"(default {DEFAULT_TIMEOUT_SECS})")
        sp.add_argument("--heartbeat", type=int, default=30,
                        help="seconds between in-flight progress lines "
                             "(default 30; 0 disables)")
        if with_checks:
            sp.add_argument("--check", action="append", default=[],
                            metavar='"<repo>:<cmd> | <cmd>"',
                            help="command run in the run's worktree(s) after "
                                 "GLM finishes; prefix '<repo>:<cmd>' to "
                                 "scope it to one repo. REPEAT per command. "
                                 "A failed check never changes the outcome.")
            sp.add_argument("--quota-endpoint", default=QUOTA_ENDPOINT_DEFAULT,
                            help=argparse.SUPPRESS)

    pr = sub.add_parser("run", help="start a delegated task")
    pr.add_argument("--brief", required=True, help="path to the task brief file")
    pr.add_argument("--repo", action="append", default=[], metavar="DIR",
                    help="repository to work in; REPEAT per repo (default: "
                         "the git repo containing the current directory)")
    pr.add_argument("--model", choices=["GLM-5.3", "GLM-5.3-Flash"],
                    help="model for the run (default: delegate.model in "
                         "~/.countersign/config.json, else the current pin "
                         "is left untouched)")
    pr.add_argument("--effort", metavar="VARIANT",
                    help="reasoning effort (default: delegate.effort, else "
                         f"{DEFAULT_EFFORT})")
    pr.add_argument("--setup", action="append", default=[],
                    metavar='"<repo>:<cmd> | <cmd>"',
                    help="command run in the worktree BEFORE GLM starts "
                         "(e.g. 'pnpm install --frozen-lockfile --offline'); "
                         "a failed setup stops the run before GLM")
    pr.add_argument("--on-limit", choices=["return", "wait"], default="return",
                    help="what to do when the z.ai usage limit hits mid-run "
                         "(default return: stop and report, resume later)")
    pr.add_argument("--max-waits", type=int, default=1,
                    help="max sleeps under --on-limit wait (default 1)")
    pr.add_argument("--mode", choices=["yolo", "guarded", "edit"],
                    default="yolo", help="ZCode permission mode (default yolo)")
    common(pr)

    ps = sub.add_parser("resume", help="continue a run's GLM session")
    ps.add_argument("run_id")
    ps.add_argument("--prompt",
                    help="override the continuation prompt (the engine "
                         "passes reviewer objections here)")
    common(ps)

    pst = sub.add_parser("status", help="report a run's state")
    pst.add_argument("run_id")

    pa = sub.add_parser("apply",
                        help="land the run's diff in the caller's tree "
                             "(uncommitted; never pushes)")
    pa.add_argument("run_id")
    pa.add_argument("--force", action="store_true",
                    help="apply even where the caller has uncommitted "
                         "changes in the same paths")

    pc = sub.add_parser("clean", help="remove the run's worktrees and branches")
    pc.add_argument("run_id")
    pc.add_argument("--force", action="store_true",
                    help="remove even when unapplied work remains in the "
                         "worktree")

    args = p.parse_args(argv)
    if hasattr(args, "heartbeat"):
        set_heartbeat_secs(max(0, args.heartbeat))
    config = load_plugin_config()
    try:
        if args.cmd == "run":
            return run_command(args, config)
        if args.cmd == "resume":
            return resume_command(args, config)
        if args.cmd == "status":
            return status_command(args, config)
        if args.cmd == "apply":
            return apply_command(args, config)
        if args.cmd == "clean":
            return clean_command(args, config)
    except KeyboardInterrupt:
        log("interrupted: zcode config originals restored")
        rep = DelegateReport(outcome="interrupted", run_id=_CURRENT_RUN_ID,
                             error="delegate run interrupted (signal); the "
                                   "zcode config originals were restored - "
                                   "re-run 'resume <run-id>' to continue")
        print(json.dumps(asdict(rep), separators=(",", ":")), flush=True)
        return EXIT_ERR
    except OrchestratorError as e:
        log(f"ERROR {e}")
        rep = DelegateReport(outcome="failed", run_id=_CURRENT_RUN_ID,
                             error=str(e)[:500])
        print(json.dumps(asdict(rep), separators=(",", ":")), flush=True)
        return EXIT_ERR
    return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
