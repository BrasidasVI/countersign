#!/usr/bin/env python3
"""countersign_loop - the dual-agent consensus engine behind the /countersign
Claude Code plugin.

Role division:
  - The INTERACTIVE Claude Code session (the human's chat) is the drafter of
    record: it holds the conversation context and owns the plan document.
  - This engine runs the consensus loop headlessly: ZCode (GLM) reviews the
    plan; a headless claude session applies each revision round; repeat until
    the reviewer approves or --max-iterations is hit.
  - The plugin command (commands/countersign.md) is the glue: it tells the
    interactive Claude how to prepare inputs (plan file + context brief) and
    how to mediate this engine's exit states back into the conversation.

Input contract: the plan document ALREADY EXISTS on disk (the interactive
session wrote it). This engine never drafts from scratch.

Invocation (what the plugin command runs):
  python countersign_loop.py PLAN.md \
      --link-repo <backend> --link-repo <frontend> \
      [--context-brief brief.md] [--decisions decisions.json] \
      [--fork-session-id <sid>] [--implement] [--max-iterations N] ...

Output contract (stable, machine-read by the plugin command):
  - stdout: exactly ONE compact JSON line, the RunReport, on every exit path.
  - stderr: human-readable progress logs (also streamed to the chat).
  - exit codes: 0 consensus | 2 error/rate-limited | 3 no-consensus |
    4 blocked-on-human (questions need answers -> --decisions) |
    5 blocked-on-branch (implement target on main/master; nothing edited).

Human-in-the-loop surfaces (all mediated by the interactive session):
  - Open questions (product/company decisions): the loop STOPS, exit 4,
    questions land in <history>/open-questions.json; the chat collects the
    human's answers, writes decisions.json, re-runs with --decisions.
  - Branch safety: implement passes refuse main/master before any edit.
  - Git: nothing is ever committed or pushed by this engine.

Usage limits (Claude Pro / z.ai 5h+weekly windows):
  - per-call rate/quota detection with exponential backoff (--max-retries);
    an un-clearable limit exits outcome="rate-limited" with the run's state
    persisted (plan snapshots + sessions in <history>) so a later re-run
    continues rather than re-spending earlier iterations.
  - a pre-run cost estimate is logged; per-agent token totals are reported.
  - --fork-session-id re-sends the forked conversation's full context on
    EVERY revise call - opt in per run, it multiplies claude-side cost.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

EXIT_CONSENSUS = 0
EXIT_ERROR = 2
EXIT_NO_CONSENSUS = 3
EXIT_BLOCKED_ON_HUMAN = 4
EXIT_BLOCKED_ON_BRANCH = 5

DEFAULT_MAX_ITERATIONS = 5
DEFAULT_TIMEOUT_SECS = 900
DEFAULT_HEARTBEAT_SECS = 30
HEARTBEAT_SECS = DEFAULT_HEARTBEAT_SECS   # set per-run from --heartbeat in main()


class OrchestratorError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# CLI / credential resolution
# --------------------------------------------------------------------------

def resolve_zcode_cmd(explicit: Optional[str], dry_run: bool) -> list[str]:
    """Return the command prefix that launches the ZCode CLI, e.g. [node, zcode.cjs].

    The headless CLI bundled with the ZCode desktop app is preferred on every
    OS; a `zcode` on PATH is only a last resort (on Linux that is usually the
    Electron desktop binary, which does not serve headless prompts - it spews
    startup logs onto stdout and never emits the JSON reply).
    """
    if explicit:
        return _zcode_explicit_cmd(explicit)
    here = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    program_files = os.environ.get("ProgramFiles") or r"C:\Program Files"
    candidates = [
        # Windows per-user install (electron-builder default)
        Path(here) / "Programs" / "ZCode" / "resources" / "glm" / "zcode.cjs",
        # Windows machine-wide install
        Path(program_files) / "ZCode" / "resources" / "glm" / "zcode.cjs",
        # Linux installer (/opt is where the official package puts the app)
        Path("/opt/ZCode/resources/glm/zcode.cjs"),
        # macOS system-wide and per-user installs (Electron convention)
        Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"),
        Path.home() / "Applications" / "ZCode.app" / "Contents" / "Resources" / "glm" / "zcode.cjs",
        # Portable / manual placements
        Path.home() / ".zcode" / "cli" / "zcode.cjs",
    ]
    for c in candidates:
        if c.is_file():
            return _node_prefix(str(c))
    if shutil.which("zcode"):
        return ["zcode"]
    if dry_run:
        return ["node", "<path-to-zcode.cjs>"]
    raise OrchestratorError(
        "Could not locate the ZCode CLI. Prerequisite missing? Install the ZCode "
        "desktop app from https://z.ai and log in once (its bundled CLI is what we "
        "invoke), or pass --zcode-cli <path to zcode.cjs> "
        f"(also looked in: {', '.join(str(c) for c in candidates)})."
    )


def _node_prefix(cjs_path: str) -> list[str]:
    node = shutil.which("node")
    if not node:
        raise OrchestratorError("node is required to run zcode.cjs but was not found on PATH.")
    return [node, cjs_path]


def _zcode_explicit_cmd(explicit: str) -> list[str]:
    # A .cjs bundle needs node; a .py needs this interpreter (keeps stub-agent
    # testing free of cmd.exe wrapper quoting); anything else runs as-is.
    if explicit.lower().endswith(".cjs"):
        return _node_prefix(explicit)
    if explicit.lower().endswith(".py"):
        return [sys.executable, explicit]
    return [explicit]


def resolve_claude_cmd(explicit: Optional[str], dry_run: bool) -> list[str]:
    if explicit:
        if explicit.lower().endswith(".py"):
            return [sys.executable, explicit]
        return [explicit]
    found = shutil.which("claude")
    # The Windows installer may ship a .cmd shim, a .exe, or both. Either launches
    # fine via subprocess in most setups; prefer .exe when present, and let
    # `--preflight` prove launchability empirically rather than trusting the guess.
    exe = shutil.which("claude.exe")
    if exe:
        return [exe]
    if found:
        return [found]
    if dry_run:
        return ["claude"]
    raise OrchestratorError(
        "Could not locate the claude CLI. Prerequisite missing? Install Claude Code "
        "from https://claude.ai/code and log in once ('claude' at a terminal), "
        "or pass --claude-cli <path>."
    )


# The bundled headless CLI requires an explicit model provider in
# ~/.zcode/cli/config.json before it will answer --prompt (the desktop app's
# login alone is not enough). These are the documented defaults, verified
# against ZCode 0.16.5; no secrets live here - the API key still flows via
# ZCODE_API_KEY, which the engine extracts from the desktop app's config.
ZCODE_CLI_CONFIG_DEFAULTS = {
    "provider": {
        "zai-coding-plan": {
            "kind": "anthropic",
            "options": {"baseURL": "https://api.z.ai/api/anthropic"},
        }
    },
    "model": {"main": "zai-coding-plan/GLM-5.3"},
}


def _is_zcode_cli_config_error(stderr: str) -> bool:
    """Recognize the bundled CLI's 'config incomplete' startup errors."""
    s = stderr or ""
    return "Model config is missing" in s or "is missing baseURL" in s


def heal_zcode_cli_config() -> Optional[list]:
    """Merge the documented model/provider defaults into ~/.zcode/cli/config.json.

    Never overwrites existing values and never writes secrets. Returns the
    list of keys added ([] when nothing was missing), or None when the file
    could not be read or has an unexpected shape (in which case it is left
    untouched for the human to fix).
    """
    path = Path.home() / ".zcode" / "cli" / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(cfg, dict):
            return None
        added = []
        model = cfg.get("model")
        if model is None:
            cfg["model"] = {"main": ZCODE_CLI_CONFIG_DEFAULTS["model"]["main"]}
            added.append("model.main")
        elif isinstance(model, dict) and not model.get("main"):
            model["main"] = ZCODE_CLI_CONFIG_DEFAULTS["model"]["main"]
            added.append("model.main")
        # a plain-string model ("provider/model") is already a valid pin; keep it
        defaults_entry = ZCODE_CLI_CONFIG_DEFAULTS["provider"]["zai-coding-plan"]
        provider = cfg.get("provider")
        if provider is None:
            provider = cfg["provider"] = {}
        if isinstance(provider, dict):
            entry = provider.get("zai-coding-plan")
            if entry is None:
                entry = provider["zai-coding-plan"] = {}
            if isinstance(entry, dict):
                if not entry.get("kind"):
                    entry["kind"] = defaults_entry["kind"]
                    added.append("provider.zai-coding-plan.kind")
                opts = entry.get("options")
                if opts is None:
                    opts = entry["options"] = {}
                if isinstance(opts, dict) and not opts.get("baseURL"):
                    opts["baseURL"] = defaults_entry["options"]["baseURL"]
                    added.append("provider.zai-coding-plan.options.baseURL")
        if added:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        return added
    except (OSError, ValueError):
        return None


def zcode_api_key() -> Optional[str]:
    """ZCODE_API_KEY env var wins; otherwise reuse the desktop app's provider key.

    The key is never written to logs, stdout, or files by this script.
    """
    env_key = os.environ.get("ZCODE_API_KEY")
    if env_key:
        return env_key
    v2 = Path.home() / ".zcode" / "v2" / "config.json"
    try:
        cfg = json.loads(v2.read_text(encoding="utf-8"))
        providers = cfg.get("provider", {})
        for pid in ("builtin:zai-coding-plan", "builtin:bigmodel-coding-plan"):
            key = (providers.get(pid, {}).get("options", {}) or {}).get("apiKey")
            if key:
                return key
        for p in providers.values():  # fallback: first provider that has a key
            key = (p.get("options", {}) or {}).get("apiKey")
            if key:
                return key
    except (OSError, ValueError):
        pass
    return None


# --------------------------------------------------------------------------
# Synthetic workspace (both repos visible, nothing else granted)
# --------------------------------------------------------------------------

def _remove_link(link: Path) -> None:
    """Remove a junction/symlink WITHOUT touching its target's contents.
    shutil.rmtree must never be used here: on Windows it follows junctions."""
    try:
        os.lstat(link)          # raises FileNotFoundError if nothing is there
    except OSError:
        return
    if link.is_symlink() or link.is_file():
        link.unlink()
    else:
        os.rmdir(link)          # junction / dir-symlink: removes the link only


def build_workspace(repo_paths: list[Path]) -> Path:
    """Build (or reuse) ~/.countersign/ws/<hash>/ containing a link to each repo.

    Pure Python on POSIX (symlinks); PowerShell junctions on Windows (junctions
    need no developer mode and no admin). Agents run with cwd = this workspace,
    so they see exactly the linked repos and nothing else beside them.
    """
    resolved = [p.resolve() for p in repo_paths]
    for p in resolved:
        if not p.is_dir():
            raise OrchestratorError(f"--link-repo {p} is not a directory")
    key = hashlib.md5("|".join(sorted(str(p) for p in resolved)).encode()).hexdigest()[:10]
    ws = Path.home() / ".countersign" / "ws" / key
    ws.mkdir(parents=True, exist_ok=True)
    for rp in resolved:
        link = ws / rp.name
        if link.exists() and link.is_dir():
            continue            # healthy link already in place
        _remove_link(link)      # dangling/damaged link: drop and recreate
        if os.name == "nt":
            ps = ("New-Item -ItemType Junction -Path "
                  f"'{str(link)}' -Target '{str(rp)}' | Out-Null")
            try:
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True, text=True, timeout=60)
            except (subprocess.TimeoutExpired, OSError) as e:
                if link.exists() and link.is_dir():
                    pass            # lost a creation race with a concurrent run; link is healthy
                else:
                    raise OrchestratorError(f"could not create junction {link} -> {rp}: {e}")
            if proc.returncode != 0 and not (link.exists() and link.is_dir()):
                raise OrchestratorError(
                    f"could not create junction {link} -> {rp}: "
                    f"{(proc.stderr or '').strip()[:200]}")
        else:
            try:
                os.symlink(rp, link)
            except FileExistsError:
                if not link.is_dir():
                    raise OrchestratorError(f"could not create symlink {link} -> {rp}")
            except OSError as e:
                raise OrchestratorError(f"could not create symlink {link} -> {rp}: {e}")
    return ws


def discover_current_claude_session(cwd: Path) -> Optional[str]:
    """Best-effort session ID of the CURRENT interactive Claude Code session.

    Claude Code stores one transcript per session at
    ~/.claude/projects/<cwd with non-alphanumerics as '-'>/<session-uuid>.jsonl;
    the live session's transcript is the most recently modified one. Used by
    --fork-current-session so the headless revise sessions can fork this
    conversation's context. Returns None when nothing plausible is found."""
    root = Path.home() / ".claude" / "projects"
    encoded = re.sub(r"[^A-Za-z0-9-]", "-", str(cwd))
    proj = root / encoded
    if not proj.is_dir():
        return None
    try:
        newest = max(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    except ValueError:
        return None
    try:
        first = newest.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        sid = json.loads(first).get("sessionId")
        return str(sid) if sid else None
    except (OSError, IndexError, ValueError):
        return None


# --------------------------------------------------------------------------
# Review invariants
# --------------------------------------------------------------------------

# Substantive consensus criteria: without these, live testing showed the reviewer
# letting "no new tests needed" pass as a minor nit and "deploy directly to
# production" go entirely unchallenged. Invariant violations are ALWAYS blocking.
DEFAULT_REVIEW_RULES = """\
- Test coverage: the plan must justify its test strategy; "no tests needed" is \
only acceptable with a concrete argument for why the change cannot regress.
- Rollout safety: the plan must not deploy or merge directly to production; \
rollout must go through a non-production branch or environment first.
- Repo verification: the plan must not assume specific files, functions, or \
frameworks exist without stating how that will be verified in the repository \
before implementation.
"""


def load_review_rules(path: Optional[str], replace_defaults: bool = False) -> str:
    """Rules from --review-rules ADD to the built-in invariants by default, so a \
    project checklist (e.g. a Ladderly one) layers on top of them; pass \
    --replace-default-rules to use the file's rules alone."""
    if not path:
        return DEFAULT_REVIEW_RULES
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise OrchestratorError(f"Could not read --review-rules file {path}: {e}")
    if not text:
        raise OrchestratorError(f"--review-rules file {path} is empty")
    if replace_defaults:
        return text + "\n"
    return DEFAULT_REVIEW_RULES + "\n" + text + "\n"


# --------------------------------------------------------------------------
# Agent calls
# --------------------------------------------------------------------------

@dataclass
class AgentResult:
    ok: bool
    text: str = ""
    session_id: Optional[str] = None
    usage: dict = field(default_factory=dict)
    stderr: str = ""
    rate_limited: bool = False


# Quota/rate-limit detection markers. The provider-side strings come from the
# ZCode bundle's own error taxonomy (insufficient_quota, credit_balance_exhausted,
# exceeded_current_quota_error, organization_usage_limit_exceeded, ...) plus the
# usual HTTP/Anthropic/OpenAI phrasings; "resets at" catches Claude Code's
# "usage limit ... resets at 7pm" subscription-window message.
RATE_LIMIT_MARKERS = (
    "rate limit", "rate_limit", "rate_limited", "ratelimit", "too many requests",
    "insufficient_quota", "credit_balance_exhausted", "exceeded_current_quota_error",
    "organization_usage_limit_exceeded", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "quota", "usage limit", "usage_limit",
    "limit reached", "limit exceeded", "spend limit", "billing hard limit",
    "resets at",
)


def _looks_rate_limited(stderr_text: str) -> bool:
    blob = (stderr_text or "").lower()
    if re.search(r"\b429\b", blob):   # word-bounded: avoids matching inside token counts
        return True
    return any(m in blob for m in RATE_LIMIT_MARKERS)


def _invoke_with_retries(fn, *, cfg, label: str) -> AgentResult:
    """Run an agent call, retrying with exponential backoff on rate/quota signals.

    Short 429s clear within the backoff window. A spent 5h/weekly coding-plan
    window will NOT clear - it exhausts the retries and returns with
    rate_limited=True so callers exit with an honest "rate-limited" outcome
    instead of a generic failure.
    """
    attempt = 0
    while True:
        try:
            res = fn()
        except OrchestratorError as e:
            res = AgentResult(ok=False, stderr=str(e))
        if res.ok or not _looks_rate_limited(res.stderr):
            return res
        if attempt >= cfg.max_retries:
            res.rate_limited = True
            return res
        delay = cfg.retry_base_delay * (2 ** attempt)
        log(f"{label}: rate/quota limited, backing off {delay}s "
            f"(retry {attempt + 1}/{cfg.max_retries}): {res.stderr[:120]}")
        time.sleep(delay)
        attempt += 1


def _run(cmd: list[str], *, timeout: int, cwd: Path, env_extra: dict, stdin_text: str = "") -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(env_extra)
    # Heartbeat: a long model call (reviews, revise passes, implement passes) can
    # run for minutes with the parent silent; periodic progress lines make a live
    # run distinguishable from a hung one.
    label = _preview_cmd(cmd)
    beat_secs = max(5, HEARTBEAT_SECS)
    _stop = threading.Event()

    def _beat() -> None:
        n = 0
        while not _stop.wait(beat_secs):
            n += 1
            log(f"... in flight: {label} ({n * beat_secs}s elapsed)")

    _beater = threading.Thread(target=_beat, daemon=True)
    _beater.start()
    try:
        return subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd),
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise OrchestratorError(f"Command timed out after {timeout}s: {label}")
    except OSError as e:
        raise OrchestratorError(f"Failed to launch {label}: {e}")
    finally:
        _stop.set()
        _beater.join(timeout=1)


def call_zcode(prompt: str, *, cwd: Path, cfg, attach: Optional[Path] = None,
               session_id: Optional[str] = None, mode: str = "plan") -> AgentResult:
    return _invoke_with_retries(
        lambda: _call_zcode_once(prompt, cwd=cwd, cfg=cfg, attach=attach,
                                 session_id=session_id, mode=mode),
        cfg=cfg, label="zcode")


def _loads_cli_json(text: str) -> Optional[dict]:
    """Parse CLI stdout that should be one JSON object; tolerate stray lines.

    Strict parse first; fall back to brace-slicing so a stray log line on
    stdout (e.g. an Electron app's startup logging) cannot kill an otherwise
    good agent reply.
    """
    try:
        return json.loads(text)
    except ValueError:
        return extract_json_object(text)


def _call_zcode_once(prompt: str, *, cwd: Path, cfg, attach: Optional[Path] = None,
                     session_id: Optional[str] = None, mode: str = "plan") -> AgentResult:
    cmd = cfg.zcode_cmd + [
        "--prompt", prompt,
        "--json", "--mode", mode, "--no-color",
    ]
    if attach is not None:
        cmd += ["--attach", str(attach)]
    if session_id:
        cmd += ["--resume", session_id]
    proc = _run(cmd, timeout=cfg.timeout, cwd=cwd, env_extra={"ZCODE_API_KEY": cfg.api_key or ""})
    if proc.returncode != 0:
        return AgentResult(ok=False, stderr=proc.stderr.strip()[:2000])
    data = _loads_cli_json(proc.stdout)
    if data is None:
        return AgentResult(ok=False, stderr="zcode stdout was not JSON: " + proc.stdout[:500])
    return AgentResult(
        ok=True,
        text=data.get("response", ""),
        session_id=data.get("sessionId"),
        usage=data.get("usage", {}),
    )


def call_claude(prompt: str, *, cwd: Path, cfg, session_id: Optional[str] = None,
                permission_mode: str = "plan", fork: bool = False) -> AgentResult:
    return _invoke_with_retries(
        lambda: _call_claude_once(prompt, cwd=cwd, cfg=cfg, session_id=session_id,
                                  permission_mode=permission_mode, fork=fork),
        cfg=cfg, label="claude")


def _call_claude_once(prompt: str, *, cwd: Path, cfg, session_id: Optional[str] = None,
                      permission_mode: str = "plan", fork: bool = False) -> AgentResult:
    # Prompt goes via stdin: plan documents can exceed Windows argv limits.
    cmd = cfg.claude_cmd + ["-p", "--output-format", "json"]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if session_id:
        cmd += ["--resume", session_id]
        if fork:
            # Branch the resumed session instead of appending to it: lets revise
            # calls inherit an interactive conversation's context without ever
            # touching the live session. Available since Claude Code 2.1.x.
            cmd += ["--fork-session"]
    proc = _run(cmd, timeout=cfg.timeout, cwd=cwd, env_extra={}, stdin_text=prompt)
    if proc.returncode != 0:
        return AgentResult(ok=False, stderr=proc.stderr.strip()[:2000])
    data = _loads_cli_json(proc.stdout)
    if data is not None:
        return AgentResult(
            ok=True,
            text=data.get("result", ""),
            session_id=data.get("session_id"),
            usage=data.get("usage", {}) or {},
        )
    # Non-JSON stdout: treat the whole stdout as the result text.
    return AgentResult(ok=True, text=proc.stdout.strip())


def _preview_cmd(cmd: list[str]) -> str:
    return " ".join(cmd[:2]) + f" ... ({len(cmd)} args)"


# --------------------------------------------------------------------------
# Consensus protocol
# --------------------------------------------------------------------------

def _brief_section(brief_text: str) -> str:
    return ("\n=== HUMAN CONTEXT BRIEF (written by the interactive session; "
            "authoritative intent, constraints, and in-chat decisions) ===\n"
            + brief_text.strip() + "\n=== END CONTEXT BRIEF ===\n")


def build_review_prompt(plan_name: str, rules_text: str, brief_text: str = "",
                        settled: Optional[dict] = None) -> str:
    # Prompt shape validated live against ZCode 0.16.3; the invariant block and the
    # open_questions channel were added after live testing showed plans passing with
    # unchallenged rollout risks and silently-made product decisions.
    base = (
        "You are the reviewing agent in a two-agent consensus workflow. "
        f"Review the attached plan document ({plan_name}).\n"
        "\n"
        "Respond with ONLY a single JSON object, no markdown fences, "
        "no prose before or after it:\n"
        '{"verdict": "approve" or "revise", '
        '"objections": [{"severity": "blocking" or "minor", '
        '"point": "1-3 sentences: what is wrong and why it matters", '
        '"suggestion": "the concrete change that would resolve it"}], '
        '"strengths": ["what the plan does right and why it matters - concrete validation, not flattery"], '
        '"open_questions": [{"question": "a decision only the human product owner can make", '
        '"why": "what it affects", "options": ["realistic option", "..."], '
        '"recommendation": "your recommended option and one-line reason"}], '
        '"fyi_notes": ["observation the human should be aware of, 1-2 sentences"], '
        '"repos_touched": ["name of each repo under the workspace whose files the plan modifies"], '
        '"summary": "a few sentences of overall assessment"}\n'
        "\n"
        "Rules:\n"
        "- repos_touched must list every repo (by directory name) the plan's "
        "file-level changes live in - it drives which repos get implementation "
        "passes.\n"
        '- verdict "approve" ONLY if there are zero objections of ANY severity '
        "(blocking and minor) AND open_questions is empty. If you have minor "
        'suggestions, verdict is "revise" and the drafter will address them.\n'
        "- Every objection must be concrete, actionable, and about the plan content.\n"
        "- Product and company-direction decisions (pricing, target users, scope, "
        "roadmap priorities, public commitments, vendor or cost commitments, legal "
        "or compliance posture) belong to the human, not to either agent. If the "
        "plan silently makes one, add it to open_questions. If the plan defers one, "
        "keep it in open_questions. Technical decisions are the drafter's to make - "
        "never send those to open_questions.\n"
        "- Borderline technical choices with notable cost, vendor, or architecture "
        "implications (e.g. reusing an existing integration path vs adding a new "
        "one) may be surfaced in fyi_notes so the human stays aware; fyi_notes "
        "never block consensus and never require a decision.\n"
        "- Substance first, but do flag ambiguities, contradictions, or unclear "
        "contracts that would force an implementer to guess.\n"
        "- strengths is positive validation for the HUMAN (what held up, so they "
        "stop second-guessing it) - it never affects the verdict and is never sent "
        "to the drafting agent. When you approve, list at least two concrete "
        "strengths unless there is genuinely nothing worth validating.\n"
        "- You are not just a gatekeeper: actively look for ways to IMPROVE the "
        "design - simpler alternatives, missed edge cases, a sharper test "
        "strategy, clearer contracts, better resilience or performance. Raise "
        "each as a 'minor' objection with the concrete improvement in "
        "suggestion (blocking only if the plan's approach is materially "
        "deficient). A thorough review with many suggestions beats a polite "
        "short one; approving with zero suggestions must mean you genuinely "
        "could not find any - but every suggestion must be one genuinely worth "
        "making, not a nitpick invented to avoid approving.\n"
        "\n"
        "Required invariants - a plan that violates ANY of these MUST get verdict "
        '"revise" with the violation reported as a BLOCKING objection:\n'
        f"{rules_text}"
    )
    if brief_text:
        base += "\n" + _brief_section(brief_text)
    if settled:
        base += (
            "\nSettled human decisions (authoritative; do NOT re-open these, re-ask "
            "them, or object to them):\n"
            + "".join(f"- Q: {q}\n  A: {a}\n" for q, a in settled.items())
        )
    return base


def build_revise_prompt(plan_text: str, objections_json: str, brief_text: str = "",
                        settled: Optional[dict] = None) -> str:
    base = (
        "You are the drafting agent in a two-agent consensus workflow. A reviewer "
        "examined your plan and returned the JSON below.\n"
        "\n"
        "=== CURRENT PLAN ===\n"
        f"{plan_text}\n"
        "=== END PLAN ===\n"
        "\n"
        f"=== REVIEWER OBJECTIONS (JSON) ===\n{objections_json}\n=== END OBJECTIONS ===\n"
        "\n"
        "Produce the COMPLETE revised plan document that resolves every blocking "
        "objection and every minor objection. Where a minor objection seems "
        "genuinely wrong, adjust the plan so the underlying concern is addressed "
        "anyway rather than ignoring it. Output only the revised plan document "
        "itself, no commentary."
    )
    if brief_text:
        base += "\n" + _brief_section(brief_text)
    if settled:
        base += (
            "\n=== HUMAN DECISIONS (settled by the product owner; treat as final "
            "requirements, incorporate fully, do not revisit or re-list as open "
            "questions) ===\n"
            + "".join(f"- Q: {q}\n  A: {a}\n" for q, a in settled.items())
            + "=== END DECISIONS ==="
        )
    return base


def build_implement_prompt(plan_path: Path, target_repo: Path,
                           all_targets: Optional[list] = None) -> str:
    # Absolute plan path: the implementation runs in a different working directory
    # (the workspace) than the one holding the real plan document.
    scope = ""
    if all_targets and len(all_targets) > 1:
        others = ", ".join(str(t) for t in all_targets if t != target_repo)
        scope = (
            f" This working directory ({target_repo}) is one of {len(all_targets)} "
            f"repositories this plan spans; the others ({others}) are handled in "
            "separate passes. Implement ONLY the parts of the plan that belong in "
            "this repository; record any cross-repo contracts in "
            "IMPLEMENTATION-NOTES.md rather than stubbing them."
        )
    return (
        f"Implement the plan in {plan_path} in this repository now. Follow it "
        "exactly. Where the plan is ambiguous, choose the simplest option and "
        "record the decision in IMPLEMENTATION-NOTES.md. Run the tests the plan "
        "specifies if the environment allows it." + scope
    )


def extract_json_object(text: str) -> Optional[dict]:
    """Parse a model reply that should be a single JSON object; tolerate fences."""
    text = text.strip()
    if not text:
        return None
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
    except ValueError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            v = json.loads(fenced.group(1))
            if isinstance(v, dict):
                return v
        except ValueError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            v = json.loads(text[start:end + 1])
            if isinstance(v, dict):
                return v
        except ValueError:
            pass
    return None


@dataclass
class Verdict:
    approve: bool
    raw_ok: bool
    blocking: list
    minor: list
    open_questions: list
    fyi_notes: list
    strengths: list
    repos_touched: list
    summary: str
    raw: str


def parse_verdict(text: str) -> Verdict:
    """Interpret the reviewer reply. Unparseable output is NEVER treated as approval."""
    data = extract_json_object(text)
    if data is None:
        return Verdict(False, False, [{"severity": "blocking",
                                       "point": "Reviewer output was not valid JSON; "
                                                "treated as a blocking objection for safety."}],
                       [], [], [], [], [], "unparseable reviewer output", text)
    verdict = str(data.get("verdict", "revise")).lower()
    blocking, minor = [], []
    for ob in data.get("objections", []) or []:
        if not isinstance(ob, dict):
            continue
        sev = str(ob.get("severity", "blocking")).lower()
        point = str(ob.get("point", "")).strip()
        if not point:
            continue
        # Unknown severity is counted as blocking: fail toward 'revise', never approval.
        entry = {"severity": sev, "point": point}
        suggestion = str(ob.get("suggestion", "")).strip()
        if suggestion:
            entry["suggestion"] = suggestion
        (minor if sev == "minor" else blocking).append(entry)
    open_questions = []
    for q in data.get("open_questions", []) or []:
        if not isinstance(q, dict):
            continue
        question = str(q.get("question", "")).strip()
        if not question:
            continue
        open_questions.append({
            "question": question,
            "why": str(q.get("why", "")).strip(),
            "options": [str(o).strip() for o in (q.get("options") or []) if str(o).strip()],
            "recommendation": str(q.get("recommendation", "")).strip(),
        })
    repos_touched = []
    for r in data.get("repos_touched", []) or []:
        r = str(r).strip().strip("/").lower()
        if r:
            repos_touched.append(r)
    fyi_notes = []
    for n in data.get("fyi_notes", []) or []:
        if isinstance(n, dict):
            note = str(n.get("note") or n.get("text") or "").strip()
        else:
            note = str(n).strip()
        if note:
            fyi_notes.append(note)
    strengths = []
    for s in data.get("strengths", []) or []:
        if isinstance(s, dict):
            s = str(s.get("note") or s.get("text") or s.get("point") or "").strip()
        else:
            s = str(s).strip()
        if s:
            strengths.append(s)
    # approve here covers blocking objections and open questions only; the loop
    # separately requires zero minor objections before declaring consensus (an
    # approve-with-minors verdict sends those minors back for one more revise
    # round). fyi_notes and strengths never affect approval - awareness only.
    approve = verdict == "approve" and not blocking and not open_questions
    return Verdict(approve, True, blocking, minor, open_questions, fyi_notes, strengths,
                   repos_touched, str(data.get("summary", "")), text)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

@dataclass
class Config:
    zcode_cmd: list[str]
    claude_cmd: list[str]
    api_key: Optional[str]
    repo: Path
    plan_path: Path
    history_dir: Path
    max_iterations: int
    timeout: int
    strategy: str          # "fresh" | "chained"
    dry_run: bool
    review_rules: str = ""
    brief_text: str = ""
    decisions_file: Optional[str] = None
    fork_session_id: Optional[str] = None
    max_retries: int = 2
    retry_base_delay: int = 30
    review_parse_retries: int = 1
    implement_repos: list = field(default_factory=list)
    heartbeat_secs: int = DEFAULT_HEARTBEAT_SECS


def log(msg: str) -> None:
    # Logs go to stderr so stdout stays reserved for the one-line JSON summary
    # emitted on exit (see Output contract in the module docstring).
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", file=sys.stderr, flush=True)


def snapshot(path: Path, dest_dir: Path, name: str) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def drafter(prompt: str, cfg: Config, session: Optional[str], mode: str,
            fork: bool = False) -> AgentResult:
    if cfg.dry_run:
        log(f"DRY-RUN claude (mode={mode}, resume={'yes' if session else 'no'}, "
            f"fork={'yes' if fork else 'no'}): {prompt[:120].replace(chr(10), ' ')}...")
        return AgentResult(ok=True, text="<dry-run revised plan>", session_id="dry-run")
    return call_claude(prompt, cwd=cfg.repo, cfg=cfg, session_id=session,
                       permission_mode=mode, fork=fork)


def reviewer(prompt: str, cfg: Config, session: Optional[str]) -> AgentResult:
    if cfg.dry_run:
        log(f"DRY-RUN zcode (mode=plan, resume={'yes' if session else 'no'}, "
            f"attach={cfg.plan_path.name})")
        return AgentResult(
            ok=True,
            text=('{"verdict":"revise","objections":[],"open_questions":[{"question":'
                  '"Dry-run: should this feature ship free or Pro-only?",'
                  '"why":"exercises the human resolution path",'
                  '"options":["free","pro-only"],"recommendation":"free"}],'
                  '"summary":"dry-run"}'),
            session_id="dry-run")
    return call_zcode(prompt, cwd=cfg.repo, cfg=cfg, attach=cfg.plan_path, session_id=session)


@dataclass
class RunReport:
    """Machine-readable run summary; emitted as one compact JSON line on stdout at exit."""
    outcome: str = "error"
    error: Optional[str] = None
    iterations_used: int = 0
    blocking_remaining: list = field(default_factory=list)
    minor_remaining: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)   # unresolved at exit
    fyi_notes: list = field(default_factory=list)        # awareness-only, cumulative
    strengths: list = field(default_factory=list)        # reviewer validation, cumulative, human-only
    decisions: dict = field(default_factory=dict)        # settled human decisions
    usage: dict = field(default_factory=dict)            # per-agent token totals
    preflight_failures: list = field(default_factory=list)
    plan: str = ""
    history_dir: str = ""
    strategy: str = ""
    implement_attempted: bool = False
    implement_refused: bool = False
    repos_touched: list = field(default_factory=list)
    branch_blocked_repos: list = field(default_factory=list)
    agent_models: dict = field(default_factory=dict)
    forked_from_session: Optional[str] = None
    open_questions_file: str = ""
    decisions_out: str = ""
    plan_sha256: str = ""
    plan_branch: Optional[str] = None
    plan_commit: Optional[str] = None
    warnings: list = field(default_factory=list)


def record_failure(res: AgentResult, what: str, cfg: Config, report: RunReport) -> None:
    if res.rate_limited:
        report.outcome = "rate-limited"
        report.error = (
            f"{what} hit a rate/quota limit after {cfg.max_retries} retr(ies). On "
            "5h/weekly coding-plan windows this usually means the window is spent "
            f"- re-run after it resets. Detail: {res.stderr[:300]}")
        log(f"RATE-LIMITED: {report.error}")
    else:
        report.outcome = "error"
        report.error = f"{what}: {res.stderr or 'empty response'}"
        log(f"ERROR {report.error}")


def _git_context(path: Path) -> "tuple[Optional[str], Optional[str]]":
    """(branch, commit) of the repo containing path, or (None, None)."""
    try:
        b = subprocess.run(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        c = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        branch = b.stdout.strip() if b.returncode == 0 else None
        commit = c.stdout.strip()[:12] if c.returncode == 0 else None
        return branch, commit
    except (subprocess.TimeoutExpired, OSError):
        return None, None


def _git_repo_root(p: Path) -> Optional[Path]:
    try:
        r = subprocess.run(["git", "-C", str(p), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=15)
        return Path(r.stdout.strip()) if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _git_identity(p: Path) -> Optional[str]:
    """Stable identity of the repo at p, shared across its worktrees.

    --git-common-dir is the same absolute path for every worktree of one
    repository, so a worktree checks out as 'the same repo' as its main
    checkout - which is exactly the case where a plan written in one and
    reviewed against the other can silently disagree."""
    try:
        r = subprocess.run(
            ["git", "-C", str(p), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return str(Path(r.stdout.strip()).resolve()).lower().replace("\\", "/")
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def check_plan_repo_consistency(plan_path: Path, link_repos: list) -> list:
    """Guards against reviewing a plan against the wrong code. Two cases, both
    warnings (the human decides whether to proceed):

    1. The plan's repo is the SAME repository as a linked repo but a DIFFERENT
       checkout (e.g. plan read from a worktree based on an old commit while
       the agents review the real checkout on another branch). Content can
       match while the surrounding code differs.
    2. The plan's own repository is entirely ABSENT from the linked repos -
       e.g. a repo set left over from another project. The agents cannot see
       the code the plan targets at all, and an implement pass would default
       to editing the linked (wrong) repos.

    Returns warning strings."""
    warnings = []
    plan_root = _git_repo_root(plan_path.parent)
    if plan_root is None or not link_repos:
        return warnings
    plan_ident = _git_identity(plan_root)
    _, plan_commit = _git_context(plan_root)
    plan_repo_linked = False
    for lr in link_repos:
        lroot = _git_repo_root(Path(lr))
        if lroot is None:
            continue
        if lroot.resolve() == plan_root.resolve():
            plan_repo_linked = True
            continue
        if plan_ident and _git_identity(lroot) == plan_ident:
            plan_repo_linked = True   # a checkout of the plan's repo IS present
            _, lcommit = _git_context(lroot)
            if lcommit != plan_commit:
                w = (f"plan repo checkout mismatch: the plan lives in "
                     f"{plan_root} (commit {plan_commit}) but the agents will "
                     f"review {lroot} (commit {lcommit}) - same repository, "
                     "different checkouts (worktree?). The review may judge the "
                     "plan against code it was not written against.")
                warnings.append(w)
    if not plan_repo_linked:
        warnings.append(
            f"plan repo not in workspace: the plan lives in {plan_root} but "
            f"the agents will only see {', '.join(str(Path(lr)) for lr in link_repos)}. "
            "The review cannot check the plan against the code it targets, and "
            "an implement pass would edit the linked repos instead of the "
            "plan's own. Fix the project's repo set (or --link-repo flags) "
            "unless the plan deliberately lives outside the reviewed repos.")
    return warnings


def acquire_plan_lock(history_dir: Path) -> Optional[Path]:
    """One engine run per plan at a time: the plan is revised IN PLACE and the
    history dir is shared, so two concurrent loops on the same plan would
    interleave revisions and clobber each other's iteration records. Returns
    the lock path on success, None when another run holds it. Locks older than
    12h are treated as stale (crashed run) and taken over."""
    lock = history_dir / "run.lock"
    history_dir.mkdir(parents=True, exist_ok=True)
    try:
        if lock.exists():
            age = time.time() - lock.stat().st_mtime
            if age < 12 * 3600:
                return None
            log(f"plan lock is {int(age // 3600)}h old - taking over (stale run?)")
            lock.unlink()
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}")
        return lock
    except FileExistsError:
        return None


def log_usage_estimate(cfg: Config, implement: bool) -> None:
    """Rough pre-run token budget so the human can decide whether to spend a
    5h/weekly coding-plan window on this run at all.

    zcode numbers are measured on this setup (live review calls: ~13k input each,
    roughly 80% cache-read; output was ~0.3k before 0.3.0, ~1k now that reviews
    carry full detail). claude numbers are measured from the first real
    claude-side run; refine as usage data accumulates.
    """
    n = cfg.max_iterations
    log(f"usage ESTIMATE for up to {n} review cycle(s): "
        f"zcode ~{n * 13_000:,} in / ~{n * 1_000:,} out (measured basis); "
        f"claude ~{n * 15_000:,} in / ~{n * 1_500:,} out (rough)")
    if cfg.fork_session_id:
        log("note: --fork re-sends the forked conversation's FULL context on every "
            "revise call - claude-side cost scales with that conversation's size")
    if implement:
        log("note: --implement is the budget-dominating pass on 5h/weekly coding "
            "plans (agentic, multi-turn, edits files) and is NOT included in the "
            "estimate above")


def run_loop(cfg: Config, implement: bool, report: RunReport) -> int:
    usage_by_agent: dict = {"claude": {}, "zcode": {}}
    report.usage = usage_by_agent
    if not cfg.dry_run:
        cfg.history_dir.mkdir(parents=True, exist_ok=True)
    drafter_session: Optional[str] = None   # the loop's OWN session (fork or chained)
    reviewer_session: Optional[str] = None

    def accumulate(usage: dict, agent: str) -> None:
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                usage_by_agent[agent][k] = usage_by_agent[agent].get(k, 0) + v

    settled: dict = {}   # authoritative human decisions: question -> answer
    if cfg.decisions_file:
        settled = load_decisions_file(cfg.decisions_file)
        if settled:
            log(f"loaded {len(settled)} human decision(s) from {cfg.decisions_file}")
            report.decisions = dict(settled)

    log_usage_estimate(cfg, implement)

    # The plan is the caller's input: it already exists (the interactive session
    # wrote it). There is no seed pass; this engine only reviews and revises.
    plan_text = cfg.plan_path.read_text(encoding="utf-8").strip()
    if not plan_text:
        report.error = f"plan file {cfg.plan_path} is empty"
        log(f"ERROR {report.error}")
        return EXIT_ERROR
    log(f"plan under review: {cfg.plan_path} ({len(plan_text)} chars); "
        f"agents' workspace: {cfg.repo}")

    # Continue snapshot numbering across resumed runs (rate-limit / open-question
    # exits) so plan-history stays a linear record of the whole consensus effort.
    existing = sorted(int(m.group(1)) for p in cfg.history_dir.glob("plan-v*.md")
                      if (m := re.match(r"plan-v(\d+)\.md$", p.name)))
    vnum = (existing[-1] if existing else 0)

    consensus = False
    verdict: Optional[Verdict] = None
    iterations_used = 0

    for i in range(1, cfg.max_iterations + 1):
        iterations_used = i
        report.iterations_used = i   # early returns (blocked-on-human, errors) keep the count
        log(f"iteration {i}/{cfg.max_iterations}: reviewer (zcode) examining {cfg.plan_path.name}")
        review = reviewer(build_review_prompt(cfg.plan_path.name, cfg.review_rules,
                                              cfg.brief_text, settled), cfg,
                          reviewer_session if cfg.strategy == "chained" else None)
        if not review.ok:
            record_failure(review, "reviewer call failed", cfg, report)
            return EXIT_ERROR
        accumulate(review.usage, "zcode")
        if cfg.strategy == "chained":
            reviewer_session = review.session_id
        verdict = parse_verdict(review.text)
        # An unparseable review is a REVIEWER-side failure, not a plan defect:
        # re-ask with a stricter JSON-only instruction instead of burning an
        # iteration on a meaningless "not valid JSON" objection.
        for attempt in range(1, cfg.review_parse_retries + 1):
            if verdict.raw_ok:
                break
            log(f"iteration {i}: reviewer reply was not valid JSON - re-asking "
                f"with stricter instruction (parse retry {attempt}/"
                f"{cfg.review_parse_retries})")
            retry_prompt = build_review_prompt(
                cfg.plan_path.name, cfg.review_rules, cfg.brief_text, settled) + (
                "\n\nIMPORTANT: your PREVIOUS reply was not valid JSON. Respond "
                "with ONLY the raw JSON object - no markdown fences, no prose "
                "before or after, every string properly escaped.")
            review = reviewer(retry_prompt, cfg,
                              reviewer_session if cfg.strategy == "chained" else None)
            if not review.ok:
                record_failure(review, "reviewer retry failed", cfg, report)
                return EXIT_ERROR
            accumulate(review.usage, "zcode")
            if cfg.strategy == "chained":
                reviewer_session = review.session_id
            verdict = parse_verdict(review.text)
        if not cfg.dry_run:
            (cfg.history_dir / f"review-iter-{i:02d}.json").write_text(
                json.dumps({"verdict": verdict.approve, "raw_ok": verdict.raw_ok,
                            "blocking": verdict.blocking, "minor": verdict.minor,
                            "open_questions": verdict.open_questions,
                            "fyi_notes": verdict.fyi_notes,
                            "strengths": verdict.strengths,
                            "summary": verdict.summary, "sessionId": review.session_id,
                            "raw": verdict.raw},
                           indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"iteration {i}: verdict={'APPROVE' if verdict.approve else 'REVISE'} "
            f"({len(verdict.blocking)} blocking, {len(verdict.minor)} minor, "
            f"{len(verdict.open_questions)} open questions) "
            f"- {verdict.summary[:100]}")
        for s in verdict.strengths:
            if s in report.strengths:
                continue        # already validated in an earlier iteration
            report.strengths.append(s)
            log(f"reviewer VALIDATED: {s[:120]}")
        for note in verdict.fyi_notes:
            if note in report.fyi_notes:
                continue   # already surfaced in an earlier iteration
            report.fyi_notes.append(note)
            log(f"FYI (no decision needed): {note[:120]}")

        # --- human resolution phase: product/direction decisions --------------
        if verdict.open_questions:
            fresh = [q for q in verdict.open_questions if q["question"] not in settled]
            if fresh:
                # Non-interactive by design: the interactive session (the chat the
                # plugin command runs in) collects answers and re-invokes with
                # --decisions. Questions land in open-questions.json.
                oq_path = cfg.history_dir / "open-questions.json"
                payload = [{**q, "answer": ""} for q in fresh]
                if not cfg.dry_run:
                    oq_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
                report.open_questions_file = str(oq_path)
                report.outcome = "blocked-on-human"
                report.open_questions = fresh
                log(f"BLOCKED ON HUMAN: {len(fresh)} product/direction question(s) "
                    f"written to {oq_path}. The interactive session collects answers, "
                    f"then re-runs with --decisions {oq_path}")
                return EXIT_BLOCKED_ON_HUMAN

        if verdict.approve and not verdict.minor:
            consensus = True
            break
        if verdict.approve and verdict.minor:
            log(f"reviewer approves with {len(verdict.minor)} minor suggestion(s) - "
                "consensus requires zero objections; one more revise round")

        if i == cfg.max_iterations:
            break

        objections_json = json.dumps(
            {"verdict": "revise",
             "objections": verdict.blocking + verdict.minor,
             "settled_human_decisions": [{"question": q, "answer": a}
                                         for q, a in settled.items()]}, indent=2)
        # First revise call forks the interactive conversation when asked (full
        # context, zero risk to the live session); the fork's returned session
        # then chains for later iterations under 'chained'. 'fresh' sessions get
        # plan + objections + brief inline each time.
        fork_this = bool(cfg.fork_session_id and drafter_session is None
                         and cfg.strategy == "chained")
        use_session = drafter_session if cfg.strategy == "chained" else (
            cfg.fork_session_id if fork_this else None)
        revised = drafter(build_revise_prompt(plan_text, objections_json,
                                              cfg.brief_text, settled), cfg,
                          use_session, "plan", fork=fork_this)
        if not revised.ok or not revised.text.strip():
            record_failure(revised, "drafter revise failed", cfg, report)
            return EXIT_ERROR
        accumulate(revised.usage, "claude")
        if cfg.strategy == "chained":
            drafter_session = revised.session_id
            if fork_this and revised.session_id:
                report.forked_from_session = cfg.fork_session_id
                log(f"revise session forked from interactive conversation "
                    f"{cfg.fork_session_id[:8]}... -> {revised.session_id[:8]}...")
        plan_text = revised.text.strip()
        if not cfg.dry_run:
            cfg.plan_path.write_text(plan_text + "\n", encoding="utf-8")
            vnum += 1
            snapshot(cfg.plan_path, cfg.history_dir, f"plan-v{vnum:02d}.md")
        log(f"revised plan written ({len(plan_text)} chars) -> {cfg.plan_path}")

    # --- outcome -------------------------------------------------------------
    report.iterations_used = iterations_used
    outcome = "consensus" if consensus else "no-consensus"
    report.outcome = outcome
    if not consensus and verdict is not None:
        report.blocking_remaining = verdict.blocking
        report.minor_remaining = verdict.minor
    summary = {
        "outcome": outcome,
        "strategy": cfg.strategy,
        "iterations_used": iterations_used,
        "max_iterations": cfg.max_iterations,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "usage": usage_by_agent,
        "plan": str(cfg.plan_path),
        "history_dir": str(cfg.history_dir),
    }
    if not cfg.dry_run:
        (cfg.history_dir / "run-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if not consensus:
        log(f"NO CONSENSUS after {iterations_used} iteration(s). Unresolved objections:")
        if verdict is not None:
            for ob in verdict.blocking:
                log(f"  BLOCKING: {ob['point']}")
            for ob in verdict.minor:
                log(f"  minor:    {ob['point']}")
        log(f"Full detail (per-iteration reviews, open questions, decisions): "
            f"{cfg.history_dir}/review-iter-{iterations_used:02d}.json - "
            "raise --max-iterations, loosen the plan, or resolve the objections above.")
        return EXIT_NO_CONSENSUS

    log(f"CONSENSUS reached after {iterations_used} iteration(s). Final plan: {cfg.plan_path}")

    if implement:
        # Targets are chosen by the AGENTS (reviewer verdict), overridable by
        # --implement-repo. The human only owns git state.
        targets = cfg.implement_repos or resolve_implement_targets(cfg, verdict)
        report.repos_touched = [str(t) for t in targets]
        log(f"implement targets (chosen by the agents): "
            f"{', '.join(t.name for t in targets)}")
        if cfg.dry_run:
            for t in targets:
                log(f"DRY-RUN implement pass for {t} (claude --permission-mode acceptEdits)")
        else:
            # Check ALL branches before any pass runs: a partial implementation
            # because one repo sat on main is worse than a clean block.
            blocked = [(t, _git_branch(t)) for t in targets
                       if _git_branch(t) in ("main", "master")]
            if blocked:
                for t, b in blocked:
                    log(f"implement BLOCKED: {t} is on '{b}'. "
                        "Create a feature branch (git -C <repo> checkout -b agent/plan-impl) "
                        "and re-run; nothing has been edited.")
                report.outcome = "blocked-on-branch"
                report.implement_refused = True
                report.branch_blocked_repos = [str(t) for t, _ in blocked]
                return EXIT_BLOCKED_ON_BRANCH
            for target_repo in targets:
                branch = _git_branch(target_repo)
                if branch is None:
                    log(f"note: {target_repo} is not a git repo (or git unavailable); "
                        "branch guard skipped")
                log(f"implement pass: claude is editing {target_repo} on branch "
                    f"'{branch}' (acceptEdits, changes stay UNCOMMITTED)")
                report.implement_attempted = True
                impl = call_claude(build_implement_prompt(cfg.plan_path, target_repo, targets),
                                   cwd=target_repo, cfg=cfg,
                                   session_id=None, permission_mode="acceptEdits")
                accumulate(impl.usage, "claude")
                if not impl.ok:
                    record_failure(impl, f"implement pass failed for {target_repo}", cfg, report)
                    return EXIT_ERROR
                log(f"implement pass finished for {target_repo} "
                    f"({len(impl.text)} chars of commentary); review with git diff and commit yourself")
            log("git steps are intentionally NOT automated; per repo:")
            log('  git add -A && git commit -m "implement plan (dual-agent consensus)"')
    return EXIT_CONSENSUS


# --------------------------------------------------------------------------
# Human decisions + implement targeting
# --------------------------------------------------------------------------

def load_decisions_file(path: str) -> dict:
    """Read human answers: the open-questions.json format this script writes
    (a list of {question, answer, ...}) or a plain {question: answer} mapping."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise OrchestratorError(f"Could not read --decisions file {path}: {e}")
    out: dict = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                q = str(item.get("question", "")).strip()
                a = str(item.get("answer", "")).strip()
                if q and a:
                    out[q] = a
    elif isinstance(data, dict):
        for k, v in data.items():
            if str(v).strip():
                out[str(k).strip()] = str(v).strip()
    return out


def resolve_implement_targets(cfg: "Config", verdict: Optional["Verdict"]) -> list:
    """Which repos the implement pass should edit - decided by the AGENTS, not
    the human: the reviewer's repos_touched verdict first, else all repos under
    the workspace. --implement-repo flags still override for manual control."""
    candidates = []
    if (cfg.repo / ".git").exists():
        candidates = [cfg.repo]           # single-repo workspace
    else:
        for child in sorted(cfg.repo.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and (child / ".git").exists():
                candidates.append(child)
    if not candidates:
        candidates = [cfg.repo]

    names = set()
    if verdict is not None:
        for r in verdict.repos_touched:
            names.add(r)
            names.add(r.split("/")[-1])
    if not names:
        log("implement targets: agents named no repos - defaulting to all repos "
            f"under the workspace ({', '.join(c.name for c in candidates)})")
        return candidates
    picked = [c for c in candidates
              if c.name.lower() in names or str(c).lower().replace(chr(92), "/") in names]
    if not picked:
        log("implement targets: agent-named repos matched nothing here - defaulting to all")
        return candidates
    return picked


def _git_branch(repo: Path) -> Optional[str]:
    """Current branch name, or None if not a git repo / git unavailable."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=30, cwd=str(repo),
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------

def preflight(cfg: Config, report: RunReport) -> int:
    """Live environment checks. Costs two tiny claude calls and one tiny zcode call.

    Resolves the open review questions empirically instead of by assumption:
    whether claude's launcher (.exe or .cmd shim) actually launches via subprocess,
    whether stdin prompt delivery works (including a ~10 KB payload), and whether
    the JSON output really carries the fields the loop depends on.
    """
    failures = []

    def safe(fn, *fn_args, **fn_kwargs) -> AgentResult:
        # Launch failures must fail one check, not abort the whole report.
        try:
            return fn(*fn_args, **fn_kwargs)
        except OrchestratorError as e:
            return AgentResult(ok=False, stderr=str(e))

    log("preflight: git (informational, checked against --repo)")
    branch = _git_branch(cfg.repo)
    guard_note = "  [implement pass would REFUSE on this branch]" if branch in ("main", "master") else ""
    log(f"  branch: {branch or 'not a git repo / git unavailable'}{guard_note}")

    # Agent probes run in an isolated temp dir, never in --repo: plan-mode CLIs can
    # read their working directory, and preflight must not touch the target repo's
    # files or context before the full stack is verified. No preflight check needs
    # repo context.
    with tempfile.TemporaryDirectory(prefix="countersign-preflight-") as probe:
        probe_cwd = Path(probe)
        log(f"preflight: claude CLI (launcher, stdin, JSON shape; probes in {probe})")
        ver_ok = False
        try:
            ver = _run(cfg.claude_cmd + ["--version"], timeout=60, cwd=probe_cwd, env_extra={})
            ver_ok = ver.returncode == 0
            log(f"  --version rc={ver.returncode}: {(ver.stdout or ver.stderr).strip()[:100]}")
        except OrchestratorError as e:
            log(f"  --version failed to launch: {e}")
        if not ver_ok:
            failures.append("claude launcher")

        small_err = ""
        try:
            small_proc = _run(cfg.claude_cmd + ["-p", "--output-format", "json"],
                              timeout=cfg.timeout, cwd=probe_cwd, env_extra={},
                              stdin_text="Reply with exactly the word OK and nothing else.")
            small_ok = small_proc.returncode == 0
        except OrchestratorError as e:
            small_ok, small_proc, small_err = False, None, str(e)
        if small_ok:
            try:
                data = json.loads(small_proc.stdout)
            except ValueError:
                data = {}
            text = str(data.get("result", "")).strip()
            sid = data.get("session_id")
            model = data.get("model") or next(
                (str(v) for k, v in data.items()
                 if "model" in k.lower() and isinstance(v, str) and v), None)
            if not model:
                for sf in (Path.home() / ".claude" / "settings.json",
                           Path.home() / ".claude" / "settings.local.json"):
                    try:
                        mv = json.loads(sf.read_text(encoding="utf-8")).get("model")
                    except (OSError, ValueError):
                        continue
                    if mv:
                        model = f"{mv} (default from {sf.name})"
                        break
            if text and sid:
                log(f"  small stdin prompt OK (result={text[:30]!r}, session_id=present)")
                report.agent_models["claude"] = (
                    f"claude (model: {model})" if model else
                    "claude (model: account default; pin with --model in settings)")
                log(f"  {report.agent_models['claude']}")
            else:
                failures.append("claude json shape (result/session_id)")
                log(f"  small stdin prompt OK but JSON shape unexpected "
                    f"(keys: {', '.join(data.keys()) or 'none'})")
        else:
            failures.append("claude small stdin prompt (not logged in? try: claude)")
            log(f"  small stdin prompt FAILED: {(small_err or (small_proc.stderr if small_proc else ''))[:200]}")

        big_payload = "lorem ipsum dolor sit amet " * 380  # ~10 KB
        big = safe(call_claude,
            "The text between the markers is padding, provided only for its size. "
            "Reply with exactly: BIG OK\n"
            f"<<<{big_payload}>>>",
            cwd=probe_cwd, cfg=cfg, permission_mode="plan")
        if big.ok and big.text.strip():
            log(f"  10KB stdin prompt OK (result={big.text.strip()[:30]!r})")
        else:
            failures.append("claude 10KB stdin prompt")
            log(f"  10KB stdin prompt FAILED: {big.stderr[:200]}")

        log("preflight: zcode CLI")
        if not cfg.api_key:
            failures.append("zcode api key")
            log("  no ZCODE_API_KEY and none found in ~/.zcode/v2/config.json")
        z = safe(call_zcode, "Reply with exactly the word OK and nothing else.",
                 cwd=probe_cwd, cfg=cfg, mode="plan")
        if not z.ok and _is_zcode_cli_config_error(z.stderr):
            # First run on a fresh device: the bundled CLI needs its model
            # provider config and nobody has written it yet. Merge the
            # documented defaults (never overwriting, never storing secrets)
            # and re-probe so first use just works.
            healed = heal_zcode_cli_config()
            if healed:
                log("  healed ~/.zcode/cli/config.json (added: "
                    + ", ".join(healed) + "); retrying probe")
                z = safe(call_zcode, "Reply with exactly the word OK and nothing else.",
                         cwd=probe_cwd, cfg=cfg, mode="plan")
        if z.ok and z.text.strip():
            sid = "present" if z.session_id else "MISSING"
            log(f"  headless prompt OK (response={z.text.strip()[:30]!r}, sessionId={sid})")
            if not z.session_id:
                failures.append("zcode sessionId")
            # Ground truth for model/effort: this probe's own rollout record
            try:
                roll = Path.home() / ".zcode" / "cli" / "rollout"
                rec = None
                for f in sorted(roll.glob("model-io-*.jsonl"),
                                key=lambda q: q.stat().st_mtime, reverse=True):
                    for ln in reversed(f.read_text(encoding="utf-8").splitlines()):
                        try:
                            cand = json.loads(ln)
                        except ValueError:
                            continue
                        if cand.get("sessionId") == z.session_id:
                            rec = cand
                            break
                    if rec:
                        break
                if rec:
                    m = rec.get("model", {}) or {}
                    effort = ((rec.get("request", {}).get("body", {})
                               .get("output_config", {})) or {}).get("effort")
                    desc = str(m.get("modelId", "?"))
                    if not effort:
                        try:
                            v2 = json.loads((Path.home() / ".zcode" / "v2" / "config.json")
                                            .read_text(encoding="utf-8"))
                            for prov in (v2.get("provider", {}) or {}).values():
                                ment = (prov.get("models", {}) or {}).get(desc)
                                if ment:
                                    eff = ((ment.get("reasoning", {}) or {})
                                           .get("defaultVariant"))
                                    if eff:
                                        effort = f"{eff} (model catalog default)"
                                    break
                        except (OSError, ValueError):
                            pass
                    if effort:
                        desc += f" (reasoning effort: {effort})"
                    report.agent_models["zcode"] = f"zcode {desc}"
                    log(f"  model in use: {desc}")
            except OSError:
                pass
        else:
            failures.append("zcode headless prompt")
            log(f"  headless prompt FAILED: {z.stderr[:200]}")

    if failures:
        log(f"preflight FAILED: {', '.join(failures)}")
        report.outcome, report.preflight_failures = "preflight-failed", failures
        return EXIT_ERROR
    log("preflight OK: both agents reachable, stdin delivery and JSON shapes verified")
    report.outcome = "preflight-ok"
    return EXIT_CONSENSUS


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="countersign consensus engine: review an existing plan with ZCode (GLM), "
                    "revise it with headless claude, loop until consensus. "
                    "Invoked by the /countersign Claude Code plugin command.")
    p.add_argument("plan_file", help="path to the plan document to review (reviewed in place)")
    p.add_argument("--link-repo", metavar="DIR", action="append", default=[],
                   help="repository the agents may see; REPEAT per repo. A synthetic "
                        "workspace (~/.countersign/ws/<hash>) is built with junction/"
                        "symlink entries for exactly these repos. Mutually exclusive "
                        "with --repo.")
    p.add_argument("--repo", default=".",
                   help="workspace directory the agents run in (default: cwd). Use "
                        "--link-repo instead to grant exactly the listed repos.")
    p.add_argument("--history-dir", default=None,
                   help="dir for snapshots, reviews, run summary (default: "
                        "<plan dir>/.countersign/<plan name>-history)")
    p.add_argument("--context-brief", metavar="FILE",
                   help="context brief written by the interactive session (intent, "
                        "constraints, in-chat decisions); embedded in every review "
                        "and revise prompt")
    p.add_argument("--fork-session-id", metavar="SID",
                   help="fork this claude conversation for the revise calls (full "
                        "context on every revise; costs scale with that conversation)")
    p.add_argument("--fork-current-session", action="store_true",
                   help="same as --fork-session-id but auto-detects the current "
                        "interactive session from ~/.claude/projects transcripts")
    p.add_argument("--expect-sha256", metavar="HASH",
                   help="sha256 of the plan file as the invoking session read it. "
                        "The engine refuses to start if the file on disk hashes "
                        "differently - guards against reviewing a stale/wrong-branch "
                        "version (e.g. a worktree that branched from the wrong base)")
    p.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS,
                   help=f"max review->revise cycles per invocation (default {DEFAULT_MAX_ITERATIONS})")
    p.add_argument("--strategy", choices=["fresh", "chained"], default="fresh",
                   help="fresh: new session per call (default, avoids anchoring on rejected "
                        "drafts); chained: reuse sessions via --resume (cheaper, risks drift). "
                        "Required for --fork-session-id to take effect.")
    p.add_argument("--implement", action="store_true",
                   help="after consensus, let claude implement the plan (acceptEdits). "
                        "Git push is never automated.")
    p.add_argument("--implement-repo", metavar="DIR", action="append",
                   help="repository the implement pass edits when the loop workspace "
                        "spans multiple repos; REPEAT the flag for tasks spanning "
                        "several repos - each gets its own branch-guarded pass "
                        "scoped to its part of the plan")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECS,
                   help=f"per-call timeout in seconds (default {DEFAULT_TIMEOUT_SECS})")
    p.add_argument("--max-retries", type=int, default=2,
                   help="retries per agent call on rate/quota signals with exponential "
                        "backoff (default 2; 0 disables). Note a spent 5h/weekly plan "
                        "window will not clear within backoff - the run then exits "
                        "with outcome 'rate-limited'")
    p.add_argument("--retry-base-delay", type=int, default=30,
                   help="base backoff delay in seconds; doubles each retry (default 30)")
    p.add_argument("--review-parse-retries", type=int, default=1,
                   help="re-asks per iteration when the reviewer reply is not valid "
                        "JSON, instead of treating it as a blocking plan objection "
                        "(default 1; 0 restores the old behavior)")
    p.add_argument("--heartbeat", type=int, default=DEFAULT_HEARTBEAT_SECS,
                   help="seconds between in-flight progress lines during long agent "
                        f"calls (default {DEFAULT_HEARTBEAT_SECS}; 0 disables)")
    p.add_argument("--zcode-cli", help="explicit path to zcode.cjs")
    p.add_argument("--claude-cli", help="explicit path to the claude executable")
    p.add_argument("--review-rules", metavar="FILE",
                   help="file with review invariants (one bullet per line) that are "
                        "ALWAYS blocking when violated; defaults to built-in rules "
                        "(test coverage rationale, no direct production rollout, "
                        "no unverified repo assumptions). Put project rules in the "
                        "repo so they version with the code.")
    p.add_argument("--replace-default-rules", action="store_true",
                   help="use the --review-rules file INSTEAD of the built-in invariants "
                        "(default: file rules are appended to the built-ins)")
    p.add_argument("--decisions", metavar="FILE",
                   help="file of human answers to open questions: the open-questions.json "
                        "this script writes on exit 4 with 'answer' fields filled in, "
                        "or a plain {question: answer} JSON mapping")
    p.add_argument("--preflight", action="store_true",
                   help="verify both CLIs live (launcher, stdin delivery incl. a ~10KB "
                        "payload, JSON output shape) and exit without running the loop; "
                        "agent probes run in an isolated temp directory")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be called without invoking any agent")
    args = p.parse_args(argv)

    plan_path = Path(args.plan_file).resolve()
    if args.preflight:
        pass                       # plan not required for preflight
    elif not plan_path.is_file():
        print(f"error: plan file {plan_path} not found", file=sys.stderr)
        return EXIT_ERROR
    for r in args.implement_repo or []:
        if not Path(r).resolve().is_dir():
            print(f"error: --implement-repo {r} is not a directory", file=sys.stderr)
            return EXIT_ERROR

    fork_session_id = args.fork_session_id
    if args.fork_current_session:
        if fork_session_id:
            print("error: --fork-session-id and --fork-current-session are mutually exclusive",
                  file=sys.stderr)
            return EXIT_ERROR
        fork_session_id = discover_current_claude_session(Path.cwd())
        if not fork_session_id and not args.dry_run:
            log("note: --fork-current-session found no current session transcript; "
                "continuing without fork (pass --fork-session-id explicitly instead)")

    try:
        if args.link_repo and args.repo != ".":
            raise OrchestratorError("--link-repo and --repo are mutually exclusive")
        if args.link_repo:
            repo = build_workspace([Path(r) for r in args.link_repo])
            log(f"synthetic workspace (links to: "
                f"{', '.join(Path(r).name for r in args.link_repo)}): {repo}")
        else:
            repo = Path(args.repo).resolve()
            if not repo.is_dir():
                raise OrchestratorError(f"--repo {repo} is not a directory")

        history_dir = (Path(args.history_dir).resolve() if args.history_dir else
                       plan_path.parent / ".countersign" / f"{plan_path.stem}-history")

        brief_text = ""
        if args.context_brief:
            brief_path = Path(args.context_brief).resolve()
            if not brief_path.is_file():
                raise OrchestratorError(f"--context-brief {brief_path} not found")
            brief_text = brief_path.read_text(encoding="utf-8").strip()
            if not brief_text:
                raise OrchestratorError(f"--context-brief {brief_path} is empty")

        try:
            claude_cmd = resolve_claude_cmd(args.claude_cli, args.dry_run)
        except OrchestratorError as e:
            if not args.preflight:
                raise
            log(f"preflight note: {e} — continuing so the zcode side can still be checked")
            claude_cmd = ["claude"]
        cfg = Config(
            zcode_cmd=resolve_zcode_cmd(args.zcode_cli, args.dry_run),
            claude_cmd=claude_cmd,
            api_key=zcode_api_key(),
            repo=repo,
            plan_path=plan_path,
            history_dir=history_dir,
            max_iterations=max(1, args.max_iterations),
            timeout=args.timeout,
            strategy=args.strategy,
            dry_run=args.dry_run,
            review_rules=load_review_rules(args.review_rules, args.replace_default_rules),
            brief_text=brief_text,
            decisions_file=args.decisions,
            fork_session_id=fork_session_id,
            max_retries=max(0, args.max_retries),
            retry_base_delay=max(1, args.retry_base_delay),
            review_parse_retries=max(0, args.review_parse_retries),
            implement_repos=([Path(r).resolve() for r in args.implement_repo]
                             if args.implement_repo else []),
            heartbeat_secs=max(0, args.heartbeat),
        )
        log(f"drafter : {' '.join(cfg.claude_cmd)}")
        log(f"reviewer: {' '.join(cfg.zcode_cmd)}")
        if not cfg.api_key and not cfg.dry_run:
            log("WARNING: no ZCODE_API_KEY found (env or ~/.zcode/v2/config.json); "
                "reviewer calls will likely fail.")
        log(f"strategy={cfg.strategy} max-iterations={cfg.max_iterations} repo={cfg.repo}")
        global HEARTBEAT_SECS
        HEARTBEAT_SECS = cfg.heartbeat_secs
        report = RunReport(
            plan=str(cfg.plan_path), history_dir=str(cfg.history_dir),
            strategy=cfg.strategy, forked_from_session=fork_session_id)
        try:
            # Content fingerprint + git context: prove the engine is reviewing
            # the exact document the invoking session read. (Inside the try so
            # the exit-time report line still prints on a mismatch refusal.)
            if plan_path.is_file():
                report.plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
                report.plan_branch, report.plan_commit = _git_context(plan_path.parent)
            if not args.preflight:
                if args.expect_sha256 and args.expect_sha256.lower() != report.plan_sha256:
                    report.outcome = "plan-mismatch"
                    report.error = (
                        "the plan on disk does not match the version the session read "
                        f"(expected sha256 {args.expect_sha256[:12]}..., found "
                        f"{report.plan_sha256[:12]}...). You are probably on a different "
                        "branch/worktree than intended (e.g. a worktree branched from the "
                        "wrong base). Re-read the correct plan, re-hash it, and re-invoke.")
                    log(f"ERROR {report.error}")
                    log(f"plan repo context: branch={report.plan_branch} "
                        f"commit={report.plan_commit}")
                    return EXIT_ERROR
                log(f"plan sha256={report.plan_sha256[:12]}... "
                    "(verified against --expect-sha256)" if args.expect_sha256 else
                    f"plan sha256={report.plan_sha256[:12]}... (no --expect-sha256 given - "
                    "pass it to guard against stale/wrong-branch reviews)")
                log(f"plan repo: branch={report.plan_branch} commit={report.plan_commit}")
                for w in check_plan_repo_consistency(
                        plan_path, [Path(r) for r in args.link_repo]):
                    report.warnings.append(w)
                    log(f"WARNING: {w}")
            if args.preflight:
                return preflight(cfg, report)
            plan_lock = acquire_plan_lock(cfg.history_dir) if not cfg.dry_run else "dry-run"
            if plan_lock is None:
                report.outcome = "locked"
                report.error = (
                    f"another countersign run is already working on {cfg.plan_path} "
                    f"(lock: {cfg.history_dir / 'run.lock'}). Concurrent loops on the "
                    "same plan would corrupt each other's revisions - wait for it to "
                    "finish, or delete the lock file if that run crashed.")
                log(f"ERROR {report.error}")
                return EXIT_ERROR
            try:
                return run_loop(cfg, args.implement, report)
            finally:
                if plan_lock != "dry-run":
                    Path(plan_lock).unlink(missing_ok=True)
        except KeyboardInterrupt:
            report.outcome = "interrupted"
            raise
        finally:
            # stdout carries exactly one machine-readable line on every exit path.
            print(json.dumps(asdict(report), separators=(",", ":")), flush=True)
    except OrchestratorError as e:
        log(f"ERROR {e}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        log("interrupted")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
