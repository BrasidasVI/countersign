#!/usr/bin/env python3
"""zcode_common - the shared ZCode-CLI helper layer.

Everything here was moved unchanged out of countersign_loop.py (delegate plan,
section 1) so both the consensus engine (countersign_loop.py) and the delegate
runner (delegate.py) invoke the ZCode headless CLI through one code path:
command resolution, provider env pinning, config healing, the model/effort
descriptors, rate-limit detection, the heartbeat-wrapped subprocess run, and
JSON/usage extraction. countersign_loop.py re-exports every moved name, so
existing tests and callers keep working.

Heartbeat note: _run() beats from this module's HEARTBEAT_SECS, which the
engine sets per run via set_heartbeat_secs() (it used to flip its own module
global before the move).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class OrchestratorError(RuntimeError):
    pass


DEFAULT_HEARTBEAT_SECS = 30
HEARTBEAT_SECS = DEFAULT_HEARTBEAT_SECS   # set per-run via set_heartbeat_secs()


def set_heartbeat_secs(secs: int) -> None:
    """Set the heartbeat interval _run() uses (engine: --heartbeat)."""
    global HEARTBEAT_SECS
    HEARTBEAT_SECS = secs


def log(msg: str) -> None:
    # Logs go to stderr so callers' stdout stays reserved for their one-line
    # JSON summary contract.
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", file=sys.stderr, flush=True)


def _preview_cmd(cmd: list[str]) -> str:
    return " ".join(cmd[:2]) + f" ... ({len(cmd)} args)"


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
            return _node_prefix(str(_keyring_aware_cjs(c, dry_run)))
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


# --------------------------------------------------------------------------
# OS-keyring credential decryption (ZCode app 3.18.2+, measured 2026-11-16)
# --------------------------------------------------------------------------
# The app migrates the shared ~/.zcode/v2/credentials.json to OS-keyring
# encryption ("enc:v2:os:" entries, secret in the desktop keyring under the
# service "ZCode Credential Encryption"). The headless CLI decrypts those via
# a dynamic import("@napi-rs/keyring") - but the /opt bundle sits outside the
# Electron app's module tree, so the import fails standalone, the account
# providers (account:zai-*-plan) are never provisioned, and the default model
# selection cannot resolve: the CLI then dies with "Select a model before
# sending a message" and, with --verbose, hangs until killed. The fix is a
# private launch copy of the bundle with the app's @napi-rs/keyring packages
# (JS wrapper extracted from app.asar, native .node symlinked from
# app.asar.unpacked) placed on its module resolution path.
LAUNCH_ROOT = Path.home() / ".countersign" / "zcode-launch"


def _credentials_use_os_keyring() -> bool:
    try:
        blob = (Path.home() / ".zcode" / "v2" / "credentials.json") \
            .read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "enc:v2:os:" in blob


def _keyring_resolvable(cjs: Path) -> bool:
    """True when import("@napi-rs/keyring") can resolve from the bundle's dir.

    Mirrors node's node_modules walk-up: any ancestor directory of the script
    carrying node_modules/@napi-rs/keyring/package.json makes the import work.
    """
    d = cjs.parent
    while True:
        if (d / "node_modules" / "@napi-rs" / "keyring" / "package.json").is_file():
            return True
        if d.parent == d:
            return False
        d = d.parent


def _asar_read_index(asar: Path) -> Optional[tuple]:
    """Parse an Electron app.asar header -> (data-base-offset, directory tree).

    Layout: 4 little-endian uint32s (pickle framing), the last one being the
    JSON directory length; file content offsets in the tree are relative to
    the 4-byte-aligned end of that JSON blob. Returns None when unreadable.
    """
    import struct
    try:
        with asar.open("rb") as f:
            head = f.read(16)
            if len(head) < 16:
                return None
            json_len = struct.unpack("<IIII", head)[3]
            raw = f.read(json_len)
            if len(raw) != json_len:
                return None
            tree = json.loads(raw.decode("utf-8"))
            base = 16 + json_len + ((-json_len) % 4)
            return base, tree
    except (OSError, ValueError, struct.error):
        return None


def _build_keyring_launch_dir(cjs: Path) -> Optional[Path]:
    """Materialize ~/.countersign/zcode-launch/<bundle-key>/ and return its cjs.

    The launch copy keeps the app's glm/config sibling layout (so the builtin
    provider-catalog probe next to the bundle still works) and adds
    node_modules/@napi-rs/keyring* from the app's asar. Reused while the
    bundle's size+mtime key matches; rebuilt after a ZCode app update.
    Returns None when the app layout does not match expectations (caller
    then falls back to the original bundle and its usual error path).
    """
    resources = cjs.parent.parent          # <app>/resources
    asar = resources / "app.asar"
    unpacked_root = resources / "app.asar.unpacked"
    if not (asar.is_file() and unpacked_root.is_dir()):
        return None
    parsed = _asar_read_index(asar)
    if parsed is None:
        return None
    base, tree = parsed
    try:
        napi = tree["files"]["node_modules"]["files"]["@napi-rs"]["files"]
    except (KeyError, TypeError):
        return None

    def rel_files(pkg_files: dict, prefix: str):
        for fname, meta in (pkg_files or {}).items():
            rel = f"{prefix}/{fname}"
            if meta.get("unpacked"):
                yield rel, ("link", unpacked_root / rel, None)
            elif "offset" in meta and "size" in meta:
                yield rel, ("extract", base + int(meta["offset"]), int(meta["size"]))

    entries = {}
    for pkg, node in sorted(napi.items()):
        if not pkg.startswith("keyring"):
            continue
        for rel, spec in rel_files(node.get("files"), f"node_modules/@napi-rs/{pkg}"):
            entries[rel] = spec
    if "node_modules/@napi-rs/keyring/index.js" not in entries:
        return None

    st = cjs.stat()
    key = f"{st.st_size}-{int(st.st_mtime_ns)}"
    launch_dir = LAUNCH_ROOT / key
    launch_cjs = launch_dir / "glm" / "zcode.cjs"
    marker = launch_dir / ".ready"
    if marker.is_file() and launch_cjs.is_file():
        return launch_cjs

    import tempfile
    LAUNCH_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=LAUNCH_ROOT) as td:
        tmp = Path(td)
        (tmp / "glm").mkdir(parents=True)
        shutil.copy2(cjs, tmp / "glm" / "zcode.cjs")
        with asar.open("rb") as f:
            for rel, (kind, a, b) in sorted(entries.items()):
                dest = tmp / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if kind == "link":
                    try:
                        os.symlink(a, dest)
                        continue
                    except OSError:
                        pass           # Windows / restricted fs: copy instead
                    if not Path(a).is_file():
                        continue
                    shutil.copy2(a, dest)
                else:
                    f.seek(a)
                    data = f.read(b)
                    if len(data) != b:
                        raise OSError(f"short read for {rel} in {asar}")
                    dest.write_bytes(data)
        # keep the builtin provider catalog probe (sibling config/) working and
        # live: symlink when possible, else copy the one file that is probed
        cfg_link = tmp / "config"
        try:
            os.symlink(resources / "config", cfg_link)
        except OSError:
            src = resources / "config" / "provider" / "zcode-builtin.json"
            if src.is_file():
                (tmp / "config" / "provider").mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, tmp / "config" / "provider" / "zcode-builtin.json")
        (tmp / ".ready").write_text("ok\n", encoding="utf-8")
        try:
            os.rename(tmp, launch_dir)
        except OSError:
            if not (marker.is_file() and launch_cjs.is_file()):
                return None        # lost a concurrent build AND it is incomplete
    log(f"zcode launch dir built (keyring shim): {launch_cjs}")
    return launch_cjs


def _keyring_aware_cjs(cjs: Path, dry_run: bool) -> Path:
    """The bundle to run: a keyring-shimmed launch copy when the app's
    os-keyring-encrypted credentials otherwise cannot be decrypted.

    No-op (returns the original) when the credentials are not os-encrypted
    (pre-3.18.2 machines), when the bundle already resolves @napi-rs/keyring,
    or when the launch dir cannot be built - the failure then surfaces through
    the CLI's own error, which _zcode_failure_hint explains.
    """
    if dry_run:
        return cjs
    try:
        if not _credentials_use_os_keyring() or _keyring_resolvable(cjs):
            return cjs
        return _build_keyring_launch_dir(cjs) or cjs
    except OSError:
        return cjs


def _zcode_explicit_cmd(explicit: str) -> list[str]:
    # A .cjs bundle needs node; a .py needs this interpreter (keeps stub-agent
    # testing free of cmd.exe wrapper quoting); anything else runs as-is.
    # Explicit .cjs paths get the same keyring-shim treatment as the bundled
    # one (a no-op unless the os-keyring credentials need it), so pointing the
    # engine at the /opt bundle directly still works on app 3.18.2+.
    if explicit.lower().endswith(".cjs"):
        return _node_prefix(str(_keyring_aware_cjs(Path(explicit), dry_run=False)))
    if explicit.lower().endswith(".py"):
        return [sys.executable, explicit]
    return [explicit]


# The bundled headless CLI requires an explicit model provider in
# ~/.zcode/cli/config.json before it will answer --prompt (the desktop app's
# login alone is not enough). These are the documented defaults, verified
# against ZCode 0.16.5; no secrets live here. Since ZCode 0.16.9 auth comes
# from a separate `zcode login` (OAuth, shared credentials in ~/.zcode/v2):
# the CLI ignores ZCODE_API_KEY, and login picks the live account:* provider
# ids, so a stale model.main here no longer breaks anything - login wins.
ZCODE_CLI_CONFIG_DEFAULTS = {
    "provider": {
        "zai-coding-plan": {
            "kind": "anthropic",
            "options": {"baseURL": "https://api.z.ai/api/anthropic"},
        }
    },
    "model": {"main": "zai-coding-plan/GLM-5.3"},
}


def _zcode_provider_env(cfg) -> dict:
    """Env that pins the CLI's provider catalogs to real files.

    Invoked bare, zcode.cjs resolves the built-in catalog by probing paths
    relative to its own location; the guess overshoots on the Linux /opt
    install (five directories up lands at /config/provider/...) and kills
    the run with "cannot locate CLI ZCode Built-in Provider Config".
    Handing it the app-shipped file removes the guess.

    The 2026-10-19 rebuild additionally pairs the two catalogs: its provider
    registry rejects a builtin pin without a personal one ("...路径必须同时
    提供" - must be provided together) and, when spawning the CLI itself, it
    fills ZCODE_PERSONAL_PROVIDER_CONFIG_FILE with
    <dataBaseDir>/.zcode/v2/provider_config.json. Mirror that same default
    (only when the file exists) so nothing `zcode login` wrote is displaced;
    on a machine without it we stay builtin-only and the CLI fills the same
    default in.
    """
    if os.environ.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"):
        return {}
    for arg in cfg.zcode_cmd:
        if arg.endswith(".cjs"):
            candidate = (Path(arg).resolve().parent.parent
                         / "config" / "provider" / "zcode-builtin.json")
            if candidate.is_file():
                env = {"ZCODE_BUILTIN_PROVIDER_CONFIG_FILE": str(candidate)}
                data_base = os.environ.get("ZCODE_DATA_BASE_DIR") or str(Path.home())
                personal = Path(data_base) / ".zcode" / "v2" / "provider_config.json"
                if personal.is_file():
                    env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = str(personal)
                return env
            break
    return {}


def _zcode_failure_hint(stderr: str) -> str:
    """Prepend an actionable hint to the CLI's account/setup failures."""
    s = stderr or ""
    if "project_token_login_failed" in s or "[project-access-token]" in s:
        # The CLI mints its per-turn project token by POSTing the stored OAuth
        # token to api.z.ai/api/auth/z/login. A stale shared login is one cause
        # (fixed by `zcode login`); the 2026-10-19 rebuild also produced this
        # with a fresh login when that endpoint rejected the new token
        # ("Z.ai user information is invalid") - nothing client-side fixes that.
        return ("zcode's project-token exchange was rejected - run `zcode login` "
                "once in a terminal first; if a fresh login still leaves this "
                "error, the rejection is server-side (report to z.ai support "
                "with the traceId): " + s)
    if "Select a model before continuing" in s or \
            "Select a model before sending a message" in s:
        # 0.16.9 said "before continuing"; 3.18.2's CLI says "before sending a
        # message". The measured 3.18.2 cause: the standalone bundle cannot
        # import @napi-rs/keyring, so the os-keyring-encrypted credentials
        # stay locked, no account provider is provisioned and the default
        # model selection cannot resolve (resolve_zcode_cmd's launch copy
        # normally papers over this; ending here means the shim could not be
        # built, or the login itself is stale).
        return ("zcode headless CLI could not resolve a usable account/model - "
                "run `zcode login` once in a terminal (separate from the "
                "desktop app's login); if the login is fine (desktop app "
                "works), the standalone CLI could not decrypt the "
                "os-keyring-encrypted credentials to provision the account "
                "provider (ZCode app 3.18.2+): " + s)
    if "Built-in Provider Config" in s:
        return ("zcode cannot locate its built-in provider catalog (the CLI's "
                "path probe misses this install layout) - pass --zcode-cli "
                "<path to zcode.cjs> or set ZCODE_BUILTIN_PROVIDER_CONFIG_FILE: " + s)
    return s


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


def zcode_model_desc(session_id: Optional[str]) -> Optional[str]:
    """Ground-truth model/reasoning-effort for one zcode session.

    Read from the CLI's own rollout record (newest model-io file first) rather
    than from our config, so what we log is what actually served the request.
    Returns None when the session's record can't be found (dry-run, fresh
    machine, or a CLI that changed its rollout shape).
    """
    if not session_id:
        return None
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
                if cand.get("sessionId") == session_id:
                    rec = cand
                    break
            if rec:
                break
        if not rec:
            return None
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
        return desc
    except OSError:
        return None


def zcode_configured_model() -> Optional[str]:
    """The model pin the zcode CLI will resolve for reviewer calls
    (model.main in ~/.zcode/cli/config.json), when one is set. This is the
    configured intent; zcode_model_desc() is the ground truth after a call."""
    try:
        cfg = json.loads((Path.home() / ".zcode" / "cli" / "config.json")
                         .read_text(encoding="utf-8"))
        m = cfg.get("model")
        if isinstance(m, dict) and m.get("main"):
            return str(m["main"])
        if isinstance(m, str) and m:   # a plain string is already a valid pin
            return m
    except (OSError, ValueError):
        pass
    return None


# --------------------------------------------------------------------------
# Rate-limit detection
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# CLI JSON output parsing
# --------------------------------------------------------------------------

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


def zcode_usage_entry(data: dict) -> dict:
    """Per-call usage out of the ZCode CLI's JSON output.

    The CLI prints {"response", "sessionId", "usage"}; the engine collects one
    usage entry per reviewer call from it (delegate does the same per run).
    """
    return data.get("usage", {})
