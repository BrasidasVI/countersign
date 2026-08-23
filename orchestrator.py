#!/usr/bin/env python3
"""
orchestrator.py — dual-agent consensus loop: Claude Code drafts, ZCode (GLM) reviews.

Verified invocations (ZCode 0.16.3, Windows, live-tested):
  node zcode.cjs --prompt "..." --attach <file> --json --mode plan --no-color [--resume <sid>]
    -> stdout: single JSON object {sessionId, response, usage, projection}; exit 0 on success.

Claude side (ASSUMPTIONS — resolve empirically with `--preflight` on a machine that
has the claude CLI installed and logged in):
  - prompt is delivered via stdin (`claude -p --output-format json` with no positional)
  - stdout JSON contains at least {result, session_id}
  - the launcher may be claude.exe or a claude.cmd shim; whichever shutil.which finds
    must be launchable via subprocess (preflight proves it by actually launching it)

Flow:
  0. understanding confirmation: the drafter restates the task as a structured
     understanding (type, intent, desired outcome, out-of-scope, repos, success
     criteria); on an interactive terminal the human confirms y/n, clarifying
     until confirmed. Non-interactive runs record the (unconfirmed) statement.
     The confirmed understanding is a settled decision: it anchors the seed
     prompt AND the review prompt (the reviewer checks plan fidelity to the
     confirmed intent, not just internal soundness), and is skipped on resume.
  1. drafter (Claude Code) writes plan v1 from the seed idea          [read-only pass]
  2. reviewer (ZCode) reviews plan.md via --attach, read-only         [--mode plan],
     judged against REQUIRED INVARIANTS (see DEFAULT_REVIEW_RULES / --review-rules)
       approve + zero blocking objections + zero open questions -> consensus, stop
       objections (technical plan flaws)                         -> drafter revises, loop
       open_questions (product/company-direction decisions)      -> HUMAN RESOLUTION
       fyi_notes (awareness-only observations)                   -> logged + summarized;
                                                                     never blocks or asks
  3. human resolution phase: open questions are settled by the human — inline
     prompts on an interactive terminal, or pre-answered via --decisions FILE
     (the format of the open-questions.json this script writes). Anything left
     unresolved is written to open-questions.json and the run exits
     blocked-on-human (code 4): fill in the answers, re-run with --decisions.
     Settled decisions are authoritative: the drafter must incorporate them and
     the reviewer must not re-open them in later iterations. Re-runs with
     --decisions RESUME: if the plan file already exists, the seed pass is skipped
     and the existing plan is reviewed directly, so settled ground is never
     re-drafted (delete the plan or change --plan-out to force a fresh draft).
  4. hard cap at --max-iterations (no agent-side turn cap available: zcode's
     --max-turns flag is listed in --help but rejected by the 0.16.3 parser)
  5. optional --implement pass (claude, acceptEdits). REFUSES to run on main/master.
     Never runs git push — pushing stays with the human.

Session strategy (test both; see discussion with the other reviewer):
  fresh   (default) every call starts a new session; orchestrator passes the current
          plan + objections explicitly. No anchoring on rejected drafts.
  chained reviewer and drafter each reuse one session across iterations via --resume;
          context accumulates (cheaper via prompt caching, but risks drift).

Output contract (CI-gatable):
  Human-readable logs go to stderr. stdout carries exactly ONE line: a compact JSON
  object emitted on every exit path (consensus, no-consensus, blocked-on-human,
  error, interrupt, preflight), e.g.
    {"outcome":"no-consensus","iterations_used":4,"blocking_remaining":[...],...}
  Exit codes: 0 consensus | 2 error | 3 no-consensus | 4 blocked-on-human.
  so pipelines can gate on `python orchestrator.py ... | jq -e .outcome`.

Rate limits & usage accounting (5h/weekly coding-plan windows):
  Neither provider exposes REMAINING quota to headless CLIs (z.ai responses carry
  no x-ratelimit-* headers; Claude Pro exposes nothing headless), so the
  orchestrator accounts for CONSUMPTION instead: a pre-run token estimate is
  logged (zcode basis measured live, claude basis assumed until run one), and
  exact per-agent totals land in the stdout JSON ("usage") and run-summary.json.
  Failed calls are matched against both providers' quota-error taxonomies
  (429 / insufficient_quota / exceeded_current_quota_error / "usage limit ...
  resets at" ...) and retried with exponential backoff (--max-retries, default 2,
  --retry-base-delay, default 30s). A spent plan window will not clear within
  backoff: the run then exits with outcome "rate-limited" (still exit code 2)
  and an error saying to re-run after the window resets. Lifetime consumption
  can be audited in ~/.zcode/cli/rollout/*.jsonl (per-request token usage).

v2 design notes (agreed with the reviewing agent, NOT implemented in v1):

  Post-implementation review pass
    After consensus -> implement, run a second reviewer pass before human testing.
    Shape: `git diff HEAD` (or diff against the pre-implementation commit) written
    to a temp file; reviewer called with --attach <diff> AND --attach plan.md, with
    the prompt adjusted to "review this implementation diff for fidelity to the
    attached plan" rather than "review this plan for soundness".
    Same three-channel verdict:
      objections     - implementation diverges from plan (blocking) or minor slippage
      fyi_notes      - gray-zone deviations worth human awareness (e.g. a
                       simplification the drafter made that changes a non-critical
                       detail)
      open_questions - must NOT appear in an implementation review; if the reviewer
                       raises one, the drafter silently resolved a product decision
                       during implementation. Treat it as a blocking objection and
                       surface it to the human before testing.
    Exit paths:
      approved            -> human tests (existing flow)
      blocking objections -> optionally loop back to the implement pass with the
                             objections attached (bounded by a separate
                             --impl-max-iterations, default 2); or exit to the human
      open_questions      -> always exit to the human; never auto-loop
    Mechanically reuses call_zcode / parse_verdict / fyi_notes with no new protocol;
    the only new surface is git diff capture and a second --attach argument.
    Not in v1 because the drafter (claude) side is not yet live-tested: validate
    seed -> review -> implement first, and add this once --implement is proven on
    a real feature.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

EXIT_CONSENSUS = 0
EXIT_ERROR = 2
EXIT_NO_CONSENSUS = 3
EXIT_BLOCKED_ON_HUMAN = 4

DEFAULT_MAX_ITERATIONS = 4
DEFAULT_TIMEOUT_SECS = 900


class OrchestratorError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# CLI / credential resolution
# --------------------------------------------------------------------------

def resolve_zcode_cmd(explicit: Optional[str], dry_run: bool) -> list[str]:
    """Return the command prefix that launches the ZCode CLI, e.g. [node, zcode.cjs]."""
    if explicit:
        return _node_prefix(explicit)
    here = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    candidates = [
        Path(here) / "Programs" / "ZCode" / "resources" / "glm" / "zcode.cjs",
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
        "Could not locate the ZCode CLI. Pass --zcode-cli <path to zcode.cjs> "
        f"(looked in: {', '.join(str(c) for c in candidates)})."
    )


def _node_prefix(cjs_path: str) -> list[str]:
    node = shutil.which("node")
    if not node:
        raise OrchestratorError("node is required to run zcode.cjs but was not found on PATH.")
    return [node, cjs_path]


def resolve_claude_cmd(explicit: Optional[str], dry_run: bool) -> list[str]:
    if explicit:
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
        "Could not locate the claude CLI. Install Claude Code and log in "
        "(claude.ai/code), or pass --claude-cli <path>."
    )


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
        raise OrchestratorError(f"Command timed out after {timeout}s: {_preview_cmd(cmd)}")
    except OSError as e:
        raise OrchestratorError(f"Failed to launch {_preview_cmd(cmd)}: {e}")


def call_zcode(prompt: str, *, cwd: Path, cfg, attach: Optional[Path] = None,
               session_id: Optional[str] = None, mode: str = "plan") -> AgentResult:
    return _invoke_with_retries(
        lambda: _call_zcode_once(prompt, cwd=cwd, cfg=cfg, attach=attach,
                                 session_id=session_id, mode=mode),
        cfg=cfg, label="zcode")


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
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return AgentResult(ok=False, stderr="zcode stdout was not JSON: " + proc.stdout[:500])
    return AgentResult(
        ok=True,
        text=data.get("response", ""),
        session_id=data.get("sessionId"),
        usage=data.get("usage", {}),
    )


def call_claude(prompt: str, *, cwd: Path, cfg, session_id: Optional[str] = None,
                permission_mode: str = "plan") -> AgentResult:
    return _invoke_with_retries(
        lambda: _call_claude_once(prompt, cwd=cwd, cfg=cfg, session_id=session_id,
                                  permission_mode=permission_mode),
        cfg=cfg, label="claude")


def _call_claude_once(prompt: str, *, cwd: Path, cfg, session_id: Optional[str] = None,
                      permission_mode: str = "plan") -> AgentResult:
    # Prompt goes via stdin: plan documents can exceed Windows argv limits.
    cmd = cfg.claude_cmd + ["-p", "--output-format", "json"]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if session_id:
        cmd += ["--resume", session_id]
    proc = _run(cmd, timeout=cfg.timeout, cwd=cwd, env_extra={}, stdin_text=prompt)
    if proc.returncode != 0:
        return AgentResult(ok=False, stderr=proc.stderr.strip()[:2000])
    try:
        data = json.loads(proc.stdout)
        return AgentResult(
            ok=True,
            text=data.get("result", ""),
            session_id=data.get("session_id"),
            usage=data.get("usage", {}) or {},
        )
    except ValueError:
        # Non-JSON stdout: treat the whole stdout as the result text.
        return AgentResult(ok=True, text=proc.stdout.strip())


def _preview_cmd(cmd: list[str]) -> str:
    return " ".join(cmd[:2]) + f" ... ({len(cmd)} args)"


# --------------------------------------------------------------------------
# Consensus protocol
# --------------------------------------------------------------------------

def build_review_prompt(plan_name: str, rules_text: str, settled: Optional[dict] = None) -> str:
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
        '"objections": [{"severity": "blocking" or "minor", "point": "one sentence"}], '
        '"open_questions": [{"question": "a decision only the human product owner can make", '
        '"why": "what it affects", "options": ["realistic option", "..."], '
        '"recommendation": "your recommended option and one-line reason"}], '
        '"fyi_notes": ["short observation the human should be aware of"], '
        '"summary": "one sentence overall assessment"}\n'
        "\n"
        "Rules:\n"
        '- verdict "approve" ONLY if there are zero blocking objections AND '
        "open_questions is empty.\n"
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
        "- Do not review style, only substance.\n"
        "\n"
        "Required invariants - a plan that violates ANY of these MUST get verdict "
        '"revise" with the violation reported as a BLOCKING objection:\n'
        f"{rules_text}"
    )
    if settled:
        base += (
            "\nSettled human decisions (authoritative; do NOT re-open these, re-ask "
            "them, or object to them):\n"
            + "".join(f"- Q: {q}\n  A: {a}\n" for q, a in settled.items())
        )
    return base


def build_seed_prompt(idea: str, repo: Path, understanding: Optional[str] = None,
                      confirmed: bool = False) -> str:
    base = (
        "Produce a complete implementation plan document for the idea below. "
        f"The working repository is: {repo}\n"
        "\n"
        f"IDEA: {idea}\n"
        "\n"
        "The plan must include these sections: Goal; Non-goals; Pre-implementation "
        "verification steps (what to confirm in the repo before coding); File-level "
        "changes; Risks and mitigations; Test strategy; Rollout steps.\n"
        "\n"
        "Product and company-direction decisions (pricing, target users, scope "
        "boundaries, roadmap priorities, public commitments, vendor or cost "
        "commitments, legal or compliance posture) belong to the human. Do NOT "
        "decide them silently: if any are needed, end the plan with a section "
        "'## Open questions' listing each with context, realistic options, and your "
        "recommendation. Ordinary technical choices are yours - decide them and "
        "note the decision inline.\n"
        "Output only the plan document (markdown), no surrounding commentary."
    )
    if understanding:
        label = ("HUMAN-CONFIRMED TASK UNDERSTANDING (authoritative; the plan must "
                 "satisfy this exactly)" if confirmed else
                 "DRAFTER'S UNCONFIRMED UNDERSTANDING (no terminal available to "
                 "confirm it; plan against it but flag any doubts as open questions)")
        base += ("\n=== " + label + " ===\n" + understanding + "\n=== END UNDERSTANDING ===\n")
    return base


def build_revise_prompt(plan_text: str, objections_json: str,
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
        "objection and as many minor ones as is reasonable. Output only the revised "
        "plan document itself, no commentary."
    )
    if settled:
        base += (
            "\n=== HUMAN DECISIONS (settled by the product owner; treat as final "
            "requirements, incorporate fully, do not revisit or re-list as open "
            "questions) ===\n"
            + "".join(f"- Q: {q}\n  A: {a}\n" for q, a in settled.items())
            + "=== END DECISIONS ==="
        )
    return base


def build_implement_prompt(plan_path: Path) -> str:
    # Absolute plan path: when --implement-repo is used, the implementation runs in
    # a different working directory than the one holding plan.md.
    return (
        f"Implement the plan in {plan_path} in this repository now. Follow it "
        "exactly. Where the plan is ambiguous, choose the simplest option and "
        "record the decision in IMPLEMENTATION-NOTES.md. Run the tests the plan "
        "specifies if the environment allows it."
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
    summary: str
    raw: str


def parse_verdict(text: str) -> Verdict:
    """Interpret the reviewer reply. Unparseable output is NEVER treated as approval."""
    data = extract_json_object(text)
    if data is None:
        return Verdict(False, False, [{"severity": "blocking",
                                       "point": "Reviewer output was not valid JSON; "
                                                "treated as a blocking objection for safety."}],
                       [], [], [], "unparseable reviewer output", text)
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
        (minor if sev == "minor" else blocking).append({"severity": sev, "point": point})
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
    fyi_notes = []
    for n in data.get("fyi_notes", []) or []:
        if isinstance(n, dict):
            note = str(n.get("note") or n.get("text") or "").strip()
        else:
            note = str(n).strip()
        if note:
            fyi_notes.append(note)
    # approve here covers objections only; the loop separately requires open_questions
    # to be empty (via human resolution) before consensus is declared. fyi_notes
    # deliberately never affect approval - they are awareness-only.
    approve = verdict == "approve" and not blocking and not open_questions
    return Verdict(approve, True, blocking, minor, open_questions, fyi_notes,
                   str(data.get("summary", "")), text)


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
    decisions_file: Optional[str] = None
    non_interactive: bool = False
    max_retries: int = 2
    retry_base_delay: int = 30
    implement_repo: Optional[Path] = None


def log(msg: str) -> None:
    # Logs go to stderr so stdout stays reserved for the one-line JSON summary
    # emitted on exit (see Output contract in the module docstring).
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", file=sys.stderr, flush=True)


def snapshot(path: Path, dest_dir: Path, name: str) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def drafter(prompt: str, cfg: Config, session: Optional[str], mode: str) -> AgentResult:
    if cfg.dry_run:
        log(f"DRY-RUN claude (mode={mode}, resume={'yes' if session else 'no'}): "
            f"{prompt[:120].replace(chr(10), ' ')}...")
        return AgentResult(ok=True, text="<dry-run plan document>", session_id="dry-run")
    return call_claude(prompt, cwd=cfg.repo, cfg=cfg, session_id=session, permission_mode=mode)


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
    decisions: dict = field(default_factory=dict)        # settled human decisions
    usage: dict = field(default_factory=dict)            # per-agent token totals
    preflight_failures: list = field(default_factory=list)
    plan: str = ""
    history_dir: str = ""
    strategy: str = ""
    implement_attempted: bool = False
    implement_refused: bool = False
    understanding_confirmed: bool = False


UNDERSTANDING_KEY = "Task understanding confirmed by the human"


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


def build_understanding_prompt(idea: str, repo: Path, clarifications: str = "") -> str:
    base = (
        "The human operator just entered this task for a two-agent coding workflow:\n"
        f"---\n{idea}\n---\n"
        f"Workspace the agents can see (may contain multiple repos): {repo}\n"
        "\n"
        "Before any work starts, restate YOUR understanding of the task so the human "
        "can confirm it. Respond with ONLY the following structure, no commentary:\n"
        "TASK TYPE: feature | bug fix | refactor | other (pick one, one line why)\n"
        "INTENT: the problem or opportunity this addresses, one or two sentences\n"
        "DESIRED OUTCOME: what will exist or be different when this is done\n"
        "OUT OF SCOPE: what this explicitly will NOT touch\n"
        "REPOS AFFECTED: which repos under the workspace this likely touches\n"
        "SUCCESS CRITERIA: how we will know it worked\n"
        "ASSUMPTIONS TO CONFIRM: anything you had to guess\n"
    )
    if clarifications:
        base += (
            "\nThe human already clarified earlier rounds:\n"
            f"{clarifications}\n"
            "Incorporate ALL of these into the restatement - do not lose them.\n"
        )
    return base


def understanding_phase(idea: str, cfg: Config, report: RunReport,
                        settled: dict) -> "tuple[bool, Optional[str], bool]":
    """Drafter restates the task; the human confirms before loop tokens are spent.

    Returns (ok, statement, confirmed). ok=False means the drafter call failed
    (report is filled in - caller should exit). The confirmed statement is stored
    in settled under UNDERSTANDING_KEY so it flows to the reviewer automatically
    and is skipped on --decisions resume. Non-interactive runs record the
    (unconfirmed) statement as an audit artifact without blocking.
    """
    if UNDERSTANDING_KEY in settled:
        log("task understanding: already confirmed in decisions - skipping phase")
        return True, settled[UNDERSTANDING_KEY], True
    if cfg.dry_run:
        log("DRY-RUN understanding statement: <dry-run: structured restatement of the task>")
        return True, None, False

    interactive = not cfg.non_interactive and sys.stdin.isatty()
    clarifications = ""
    rounds = 0
    while True:
        res = drafter(build_understanding_prompt(idea, cfg.repo, clarifications),
                      cfg, None, "plan")
        if not res.ok or not res.text.strip():
            record_failure(res, "understanding pass failed", cfg, report)
            return False, None, False
        statement = res.text.strip()
        rounds += 1

        if not interactive:
            log("non-interactive: drafter's understanding recorded (UNCONFIRMED):")
            for line in statement.splitlines():
                log(f"  | {line}")
            (cfg.history_dir / "understanding.md").write_text(statement + "\n",
                                                              encoding="utf-8")
            return True, statement, False

        print(f"\n===== DRAFTER'S UNDERSTANDING (round {rounds}) =====",
              file=sys.stderr, flush=True)
        print(statement, file=sys.stderr, flush=True)
        print("=" * 52, file=sys.stderr, flush=True)
        try:
            ans = input("Does this match your intent? (y/n): ").strip().lower()
        except EOFError:
            ans = ""
        if ans.startswith("y"):
            settled[UNDERSTANDING_KEY] = statement
            report.decisions = dict(settled)
            report.understanding_confirmed = True
            (cfg.history_dir / "understanding.md").write_text(statement + "\n",
                                                              encoding="utf-8")
            log("task understanding CONFIRMED - it now anchors the seed and review prompts")
            return True, statement, True
        if rounds >= 8:
            log("note: 8+ clarification rounds - consider rewriting the task itself "
                "(Ctrl+C, then rerun with a clearer prompt)")
        print("Enter your clarification, one line at a time; a blank line finishes it:",
              file=sys.stderr, flush=True)
        clarify = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip() and clarify:
                break
            clarify.append(line)
        if clarify:
            clarifications += "- " + "\n- ".join(clarify) + "\n"


def log_usage_estimate(cfg: Config, implement: bool) -> None:
    """Rough pre-run token budget so the human can decide whether to spend a
    5h/weekly coding-plan window on this run at all.

    zcode numbers are measured on this setup (live review calls: ~13k input /
    ~0.3k output each, roughly 80% cache-read). claude numbers are ASSUMED until
    the first claude-side run - the post-run per-agent usage totals in the run
    summary are the honest numbers; refine these after run one.
    """
    n = cfg.max_iterations
    log(f"usage ESTIMATE for up to {n} review cycle(s): "
        f"zcode ~{n * 13_000:,} in / ~{n * 300:,} out (measured basis); "
        f"claude ~{n * 15_000:,} in / ~{n * 1_500:,} out (assumed - refine after "
        "the first claude-side run)")
    if implement:
        log("note: --implement is the budget-dominating pass on 5h/weekly coding "
            "plans (agentic, multi-turn, edits files) and is NOT included in the "
            "estimate above")


def run_loop(idea: str, cfg: Config, implement: bool, report: RunReport) -> int:
    usage_by_agent: dict = {"claude": {}, "zcode": {}}
    report.usage = usage_by_agent
    drafter_session: Optional[str] = None
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

    # --- understanding confirmation (step 0; skipped on resume) ---------------
    resume_run = (bool(cfg.decisions_file) and cfg.plan_path.exists()
                  and cfg.plan_path.stat().st_size > 0)
    understanding: Optional[str] = None
    if resume_run:
        understanding = settled.get(UNDERSTANDING_KEY)
        report.understanding_confirmed = understanding is not None
    else:
        ok, understanding, confirmed = understanding_phase(idea, cfg, report, settled)
        if not ok:
            return EXIT_ERROR
        report.understanding_confirmed = confirmed

    # --- seed: drafter produces plan v1 (skipped when resuming with decisions) ----
    if resume_run:
        plan_text = cfg.plan_path.read_text(encoding="utf-8").strip()
        log(f"resuming: reviewing existing {cfg.plan_path.name} ({len(plan_text)} chars); "
            "seed pass skipped so settled decisions are not re-drafted (delete the "
            "plan or change --plan-out to force a fresh draft)")
    else:
        log(f"seed: asking drafter (claude) for plan v1 in {cfg.repo}")
        seed = drafter(build_seed_prompt(idea, cfg.repo, understanding, report.understanding_confirmed), cfg, drafter_session if cfg.strategy == "chained" else None, "plan")
        if not seed.ok or not seed.text.strip():
            record_failure(seed, "drafter seed failed", cfg, report)
            return EXIT_ERROR
        accumulate(seed.usage, "claude")
        if cfg.strategy == "chained":
            drafter_session = seed.session_id
        plan_text = seed.text.strip()
        if not cfg.dry_run:
            cfg.plan_path.write_text(plan_text + "\n", encoding="utf-8")
            snapshot(cfg.plan_path, cfg.history_dir, "plan-v01-seed.md")
        log(f"plan v1 written ({len(plan_text)} chars) -> {cfg.plan_path}")

    consensus = False
    verdict: Optional[Verdict] = None
    iterations_used = 0

    for i in range(1, cfg.max_iterations + 1):
        iterations_used = i
        log(f"iteration {i}/{cfg.max_iterations}: reviewer (zcode) examining {cfg.plan_path.name}")
        review = reviewer(build_review_prompt(cfg.plan_path.name, cfg.review_rules, settled), cfg,
                          reviewer_session if cfg.strategy == "chained" else None)
        if not review.ok:
            record_failure(review, "reviewer call failed", cfg, report)
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
                            "summary": verdict.summary, "sessionId": review.session_id},
                           indent=2, ensure_ascii=False), encoding="utf-8")
        log(f"iteration {i}: verdict={'APPROVE' if verdict.approve else 'REVISE'} "
            f"({len(verdict.blocking)} blocking, {len(verdict.minor)} minor, "
            f"{len(verdict.open_questions)} open questions) "
            f"- {verdict.summary[:100]}")
        for note in verdict.fyi_notes:
            log(f"FYI (no decision needed): {note[:160]}")
            if note not in report.fyi_notes:
                report.fyi_notes.append(note)

        # --- human resolution phase: product/direction decisions --------------
        if verdict.open_questions:
            new_answers = resolve_open_questions(verdict.open_questions, settled, cfg, i)
            if new_answers is None:
                report.outcome = "blocked-on-human"
                report.open_questions = [
                    q for q in verdict.open_questions if q["question"] not in settled]
                return EXIT_BLOCKED_ON_HUMAN
            if new_answers:
                settled.update(new_answers)
                report.decisions = dict(settled)
                if not cfg.dry_run:
                    (cfg.history_dir / f"decisions-iter-{i:02d}.json").write_text(
                        json.dumps([{"question": q, "answer": a} for q, a in settled.items()],
                                   indent=2, ensure_ascii=False), encoding="utf-8")

        if verdict.approve:
            consensus = True
            break

        if i == cfg.max_iterations:
            break

        objections_json = json.dumps(
            {"verdict": "revise",
             "objections": verdict.blocking + verdict.minor,
             "settled_human_decisions": [{"question": q, "answer": a}
                                         for q, a in settled.items()]}, indent=2)
        revised = drafter(build_revise_prompt(plan_text, objections_json, settled), cfg,
                          drafter_session if cfg.strategy == "chained" else None, "plan")
        if not revised.ok or not revised.text.strip():
            record_failure(revised, "drafter revise failed", cfg, report)
            return EXIT_ERROR
        accumulate(revised.usage, "claude")
        if cfg.strategy == "chained":
            drafter_session = revised.session_id
        plan_text = revised.text.strip()
        if not cfg.dry_run:
            cfg.plan_path.write_text(plan_text + "\n", encoding="utf-8")
            snapshot(cfg.plan_path, cfg.history_dir, f"plan-v{i + 1:02d}.md")
        log(f"plan v{i + 1} written ({len(plan_text)} chars)")

    # --- outcome -------------------------------------------------------------
    report.iterations_used = iterations_used
    outcome = "consensus" if consensus else "no-consensus"
    report.outcome = outcome
    if not consensus and verdict is not None:
        report.blocking_remaining = verdict.blocking
        report.minor_remaining = verdict.minor
    summary = {
        "idea": idea,
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
        log(f"NO CONSENSUS after {iterations_used} iterations. "
            f"Remaining blocking objections are in {cfg.history_dir}/review-iter-{iterations_used:02d}.json. "
            "Raise --max-iterations, loosen the idea, or inspect the objections yourself.")
        return EXIT_NO_CONSENSUS

    log(f"CONSENSUS reached after {iterations_used} iteration(s). Final plan: {cfg.plan_path}")

    if implement:
        if cfg.dry_run:
            log("DRY-RUN implement pass (claude --permission-mode acceptEdits)")
        else:
            # Mechanical guard: prompt-level "target dev only" rules are advisory;
            # this check is deterministic. Refuses to let the agent edit main/master.
            # Guards the implementation target (--implement-repo when given, else
            # the loop repo) - a multi-repo workspace root is not itself a git repo.
            target_repo = cfg.implement_repo or cfg.repo
            branch = _git_branch(target_repo)
            if branch in ("main", "master"):
                log(f"implement pass REFUSED: current branch is '{branch}' in {target_repo}. "
                    "Create a feature branch first (e.g. git checkout -b agent/plan-impl).")
                report.implement_refused = True
                return EXIT_CONSENSUS
            if branch is None:
                log("note: not a git repo (or git unavailable); branch guard skipped")
            log(f"implement pass: claude is editing {target_repo} (acceptEdits)")
            report.implement_attempted = True
            impl = call_claude(build_implement_prompt(cfg.plan_path), cwd=target_repo, cfg=cfg,
                               session_id=None, permission_mode="acceptEdits")
            accumulate(impl.usage, "claude")
            if not impl.ok:
                record_failure(impl, "implement pass failed", cfg, report)
                return EXIT_ERROR
            log(f"implement pass finished ({len(impl.text)} chars of final commentary)")
            log("git steps are intentionally NOT automated; suggested:")
            log('  git checkout -b agent/plan-impl && git add -A && git commit -m "implement plan (dual-agent consensus)"')
    return EXIT_CONSENSUS


# --------------------------------------------------------------------------
# Human resolution phase
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


def _ask_human(q: dict, n: int, total: int) -> str:
    # Everything to stderr: stdout stays reserved for the exit-time JSON line.
    print(f"\n--- OPEN QUESTION {n}/{total} (human decision required) ---",
          file=sys.stderr, flush=True)
    print(f"  {q['question']}", file=sys.stderr, flush=True)
    if q.get("why"):
        print(f"  why it matters: {q['why']}", file=sys.stderr, flush=True)
    if q.get("options"):
        print(f"  options: {', '.join(q['options'])}", file=sys.stderr, flush=True)
    if q.get("recommendation"):
        print(f"  reviewer recommendation: {q['recommendation']}", file=sys.stderr, flush=True)
    try:
        return input("  your decision (free text; Enter to defer): ").strip()
    except EOFError:
        return ""


def resolve_open_questions(questions: list, settled: dict, cfg: Config,
                           iteration: int) -> Optional[dict]:
    """Get human answers for reviewer-raised open questions.

    Resolution order: questions already settled (earlier iteration or a
    --decisions file) are skipped; then inline prompting on an interactive
    terminal; anything still unresolved is written to open-questions.json and
    the caller must exit blocked-on-human so the human can answer and re-run.
    Returns {question: answer} for newly resolved questions, None if any remain
    unresolved.
    """
    fresh = [q for q in questions if q["question"] not in settled]
    if not fresh:
        return {}

    if cfg.dry_run:
        return {q["question"]: "dry-run: deferred to human" for q in fresh}

    answers: dict = {}
    if not cfg.non_interactive and sys.stdin.isatty():
        log(f"human resolution phase: {len(fresh)} open question(s) need your decisions")
        for n, q in enumerate(fresh, 1):
            ans = _ask_human(q, n, len(fresh))
            if ans:
                answers[q["question"]] = ans
        if len(answers) == len(fresh):
            return answers
        log(f"{len(fresh) - len(answers)} question(s) deferred by you")

    unresolved = [q for q in fresh if q["question"] not in answers]
    out_path = cfg.plan_path.parent / "open-questions.json"
    payload = [{**q, "answer": answers.get(q["question"], "")} for q in fresh]
    if not cfg.dry_run:
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    log(f"UNRESOLVED product/direction decisions need human input: {len(unresolved)} "
        f"question(s) written to {out_path}. Fill the 'answer' fields and re-run "
        f"with --decisions {out_path.name}")
    return None


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
    # files or context before the full stack is verified (first-run sequencing
    # concern raised in design review). No preflight check needs repo context.
    with tempfile.TemporaryDirectory(prefix="orchestrator-preflight-") as probe:
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

        small = safe(call_claude, "Reply with exactly the word OK and nothing else.",
                     cwd=probe_cwd, cfg=cfg, permission_mode="plan")
        if small.ok and small.text.strip():
            sid = "present" if small.session_id else "MISSING"
            log(f"  small stdin prompt OK (result={small.text.strip()[:30]!r}, session_id={sid})")
            if not small.session_id:
                failures.append("claude json session_id")
        else:
            failures.append("claude small stdin prompt (not logged in? try: claude)")
            log(f"  small stdin prompt FAILED: {small.stderr[:200]}")

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
        if z.ok and z.text.strip():
            sid = "present" if z.session_id else "MISSING"
            log(f"  headless prompt OK (response={z.text.strip()[:30]!r}, sessionId={sid})")
            if not z.session_id:
                failures.append("zcode sessionId")
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


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Dual-agent consensus loop: Claude Code drafts a plan, ZCode (GLM) reviews it, "
                    "loop until the reviewer approves or --max-iterations is hit.")
    p.add_argument("idea", help="seed idea for the plan")
    p.add_argument("--repo", default=".", help="repository to plan against (default: cwd)")
    p.add_argument("--plan-out", default="plan.md", help="path for the plan document")
    p.add_argument("--history-dir", default="plan-history", help="dir for snapshots, reviews, run summary")
    p.add_argument("--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS,
                   help=f"max review->revise cycles (default {DEFAULT_MAX_ITERATIONS})")
    p.add_argument("--strategy", choices=["fresh", "chained"], default="fresh",
                   help="fresh: new session per call (default, avoids anchoring on rejected "
                        "drafts); chained: reuse sessions via --resume (cheaper, risks drift)")
    p.add_argument("--implement", action="store_true",
                   help="after consensus, let claude implement the plan (acceptEdits). "
                        "Git push is never automated.")
    p.add_argument("--implement-repo", metavar="DIR",
                   help="repository the implement pass edits when the loop workspace "
                        "spans multiple repos (e.g. a dir containing separate frontend "
                        "and backend repos); the branch guard applies to THIS repo")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECS,
                   help=f"per-call timeout in seconds (default {DEFAULT_TIMEOUT_SECS})")
    p.add_argument("--max-retries", type=int, default=2,
                   help="retries per agent call on rate/quota signals with exponential "
                        "backoff (default 2; 0 disables). Note a spent 5h/weekly plan "
                        "window will not clear within backoff - the run then exits "
                        "with outcome 'rate-limited'")
    p.add_argument("--retry-base-delay", type=int, default=30,
                   help="base backoff delay in seconds; doubles each retry (default 30)")
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
    p.add_argument("--non-interactive", action="store_true",
                   help="never prompt on the terminal; unresolved open questions exit "
                        "blocked-on-human (4) with an open-questions.json to answer")
    p.add_argument("--preflight", action="store_true",
                   help="verify both CLIs live (launcher, stdin delivery incl. a ~10KB "
                        "payload, JSON output shape) and exit without running the loop; "
                        "agent probes run in an isolated temp directory, never in --repo")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be called without invoking any agent")
    args = p.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"error: --repo {repo} is not a directory", file=sys.stderr)
        return EXIT_ERROR
    if args.implement_repo and not Path(args.implement_repo).resolve().is_dir():
        print(f"error: --implement-repo {args.implement_repo} is not a directory",
              file=sys.stderr)
        return EXIT_ERROR

    try:
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
            plan_path=(repo / args.plan_out).resolve(),
            history_dir=(repo / args.history_dir).resolve(),
            max_iterations=max(1, args.max_iterations),
            timeout=args.timeout,
            strategy=args.strategy,
            dry_run=args.dry_run,
            review_rules=load_review_rules(args.review_rules, args.replace_default_rules),
            decisions_file=args.decisions,
            non_interactive=args.non_interactive,
            max_retries=max(0, args.max_retries),
            retry_base_delay=max(1, args.retry_base_delay),
            implement_repo=(Path(args.implement_repo).resolve()
                            if args.implement_repo else None),
        )
        log(f"drafter : {' '.join(cfg.claude_cmd)}")
        log(f"reviewer: {' '.join(cfg.zcode_cmd)}")
        if not cfg.api_key and not cfg.dry_run:
            log("WARNING: no ZCODE_API_KEY found (env or ~/.zcode/v2/config.json); "
                "reviewer calls will likely fail.")
        log(f"strategy={cfg.strategy} max-iterations={cfg.max_iterations} repo={cfg.repo}")
        report = RunReport(
            plan=str(cfg.plan_path), history_dir=str(cfg.history_dir),
            strategy=cfg.strategy)
        try:
            if args.preflight:
                return preflight(cfg, report)
            return run_loop(args.idea, cfg, args.implement, report)
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
