#!/usr/bin/env bash
# launch.sh — interactive launcher for the dual-agent consensus loop.
#
# First run on a device: prompts for device-local paths (saved to
# device-config.sh, which is gitignored) and runs --preflight once.
# Every run: prompts for loop configuration, then the task prompt,
# then launches orchestrator.py against the workspace containing your
# frontend and backend repos. Handles the blocked-on-human exit by
# letting you answer open-questions.json and re-running automatically.
#
# Tested on Git Bash (Windows) and should work on macOS/Linux bash.

set -euo pipefail

# Resolve our own directory WITHOUT dirname: when launched via the Windows
# .cmd shim, bash starts with the caller's PATH and Git's Unix tools are not
# on it (cd/pwd are builtins, so they always work).
SCRIPT_SOURCE="${BASH_SOURCE[0]}"
case "$SCRIPT_SOURCE" in
  */*) SCRIPT_DIR="$(cd "${SCRIPT_SOURCE%/*}" && pwd)" ;;
  *)   SCRIPT_DIR="$(pwd)" ;;
esac

# Execute from a private SNAPSHOT of this script: bash reads scripts
# incrementally, so editing launch.sh while a run is live would otherwise
# corrupt it mid-flight. The snapshot makes live edits harmless (the next
# run picks them up).
if [[ "${COUNTERSIGN_SNAP:-}" != "1" ]]; then
  _snap="$(mktemp "${TMPDIR:-/tmp}/countersign.XXXXXX.sh")" || _snap=""
  if [[ -n "$_snap" ]]; then
    cat "$SCRIPT_DIR/launch.sh" > "$_snap"
    exec env COUNTERSIGN_SNAP=1 SCRIPT_DIR_OVERRIDE="$SCRIPT_DIR" bash "$_snap" "$@"
  fi
fi
SCRIPT_DIR="${SCRIPT_DIR_OVERRIDE:-$SCRIPT_DIR}"
[[ "${COUNTERSIGN_SNAP:-}" == "1" ]] && trap 'rm -f "${BASH_SOURCE[0]}"' EXIT

CONFIG_FILE="$SCRIPT_DIR/device-config.sh"
ORCH="$SCRIPT_DIR/orchestrator.py"

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
_clean_path() { # strip surrounding quotes and leading/trailing whitespace
  # Order matters: trim spaces first (quotes may sit inside them), then quotes,
  # then spaces again (in case spaces were inside the quotes).
  local __s=$1
  __s="${__s#"${__s%%[![:space:]]*}"}"
  __s="${__s%"${__s##*[![:space:]]}"}"
  __s="${__s#\"}"; __s="${__s%\"}"
  __s="${__s#\'}"; __s="${__s%\'}"
  __s="${__s#"${__s%%[![:space:]]*}"}"
  __s="${__s%"${__s##*[![:space:]]}"}"
  printf '%s' "$__s"
}
ask()  { # ask VAR "prompt" "default" ; empty answer keeps default
  local __var=$1 __prompt=$2 __def=$3 __ans=""
  read -r -p "$__prompt [$__def]: " __ans || true
  __ans=$(_clean_path "$__ans")
  printf -v "$__var" '%s' "${__ans:-$__def}"
}
ask_dir() { # ask_dir VAR "prompt" "default" ; must be an existing dir
  local __var=$1
  ask "$@"
  while [[ ! -d "${!__var}" ]]; do
    if ! read -r -p "Directory does not exist. Path: " __tmp; then
      echo "No input available - aborting setup." >&2
      exit 1
    fi
    __tmp=$(_clean_path "$__tmp")
    printf -v "$__var" '%s' "$__tmp"
  done
}
ask_yn() { # ask_yn VAR "prompt" "y|n default"
  local __var=$1 __prompt=$2 __def=$3 __ans=""
  read -r -p "$__prompt ($([[ $__def == y ]] && echo Y/n || echo y/N)): " __ans || true
  __ans="${__ans:-$__def}"
  [[ "$__ans" == [yY]* ]] && printf -v "$__var" '%s' "y" || printf -v "$__var" '%s' "n"
}

# --- branch selection helpers (used by the blocked-on-branch resume path) ---
git_branch_of() { git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?"; }

_link_repo_into_ws() { # <wsdir> <name> <target> ; idempotent; NEVER touches target contents
  local link="$1/$2" target
  target=$(cd "$3" && pwd)
  if [[ -L "$link" ]]; then
    rm -f "$link"
  elif [[ -e "$link" ]]; then
    # existing junction/dir entry: remove the LINK only (rmdir on Windows does
    # not recurse into the target; rm -rf on a junction could - avoid it)
    case "$(uname -s)" in
      MINGW*|MSYS*|CYGWIN*) cmd //c rmdir "$(cygpath -w "$link")" >/dev/null 2>&1 ;;
      *) rm -rf "$link" ;;
    esac
  fi
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      # PowerShell junctions: cmd's mklink mangles args when invoked from Git Bash
      powershell.exe -NoProfile -Command \
        "New-Item -ItemType Junction -Path '$(cygpath -w "$link")' -Target '$(cygpath -w "$target")' | Out-Null" \
        >/dev/null 2>&1 ;;
    *) ln -s "$target" "$link" ;;
  esac
}

build_workspace() { # build_workspace <backend> <frontend> [py] ; echoes the ws path
  # Synthetic workspace: a directory containing ONLY links to the two repos, so
  # the agents get full access to exactly those repos and nothing else. Run
  # artifacts (plan.md, plan-history/) live here too, keeping repos pristine.
  local backend=$1 frontend=$2 py=${3:-python} key
  key=$("$py" -c "import hashlib,sys;print(hashlib.md5('|'.join(sys.argv[1:]).encode()).hexdigest()[:10])" \
        "$backend" "$frontend" 2>/dev/null || echo "default")
  local ws="$HOME/.countersign/ws/$key"
  mkdir -p "$ws"
  _link_repo_into_ws "$ws" backend "$backend"
  _link_repo_into_ws "$ws" frontend "$frontend"
  echo "$ws"
}

select_impl_branch() { # select_impl_branch <repo-path> ; returns 1 to abort
  local repo=$1 branch _cb NEW_BRANCH
  branch=$(git_branch_of "$repo")
  if [[ "$branch" == "?" ]]; then
    echo "warning: could not read the git branch of $repo."
    ask_yn _cb "Continue with this repo anyway (branch guard will be skipped)" "n"
    [[ "$_cb" == "n" ]] && return 1
  elif [[ "$branch" == "main" || "$branch" == "master" ]]; then
    echo "$repo is on '$branch' - countersign does not implement on main/master."
    ask NEW_BRANCH "New branch name to create and switch to" "agent/plan-impl"
    git -C "$repo" checkout -b "$NEW_BRANCH" || { echo "Branch creation failed - aborting." >&2; return 1; }
    echo "OK: $repo is now on new branch '$NEW_BRANCH'."
  else
    ask NEW_BRANCH "Branch for $repo: Enter = stay on '$branch', or type a NEW branch name" ""
    if [[ -n "$NEW_BRANCH" ]]; then
      git -C "$repo" checkout -b "$NEW_BRANCH" || { echo "Branch creation failed - aborting." >&2; return 1; }
      echo "OK: $repo is now on new branch '$NEW_BRANCH'."
    else
      echo "$repo: implementation will run on branch '$branch' (uncommitted; you review and commit)."
    fi
  fi
}

# --- 1. device-local configuration (first run only) ------------------------
if [[ ! -f "$CONFIG_FILE" ]]; then
  say "First run on this device: setup - local paths"
  echo "(Saved to $CONFIG_FILE, which is gitignored. Delete it to re-configure.)"
  echo "Press Enter to accept the default shown in brackets."
  echo ""

  # python
  PY=""
  command -v python3 >/dev/null 2>&1 && PY=python3
  [[ -z "$PY" ]] && command -v python >/dev/null 2>&1 && PY=python
  [[ -z "$PY" ]] && PY=python
  ask PY "Python command" "$PY"

  # the two repos
  ask_dir BACKEND "Path to BACKEND repo" ""
  ask_dir FRONTEND "Path to FRONTEND repo" ""

  # optional CLI overrides (empty = auto-detect)
  ask ZCODE_CLI "Path to zcode.cjs (blank = auto-detect)" ""
  ask CLAUDE_CLI "Path to claude executable (blank = auto-detect)" ""

  WS=$(build_workspace "$BACKEND" "$FRONTEND" "$PY")
  cat > "$CONFIG_FILE" <<EOF
# Device-local launcher config (gitignored). Regenerate by deleting this file.
BACKEND=$(printf '%q' "$BACKEND")
FRONTEND=$(printf '%q' "$FRONTEND")
WORKSPACE=$(printf '%q' "$WS")
PY=$(printf '%q' "$PY")
ZCODE_CLI=$(printf '%q' "$ZCODE_CLI")
CLAUDE_CLI=$(printf '%q' "$CLAUDE_CLI")
PREFLIGHT_DONE=no
EOF
  echo ""
  say "Saved device config. (Backend: $BACKEND | Frontend: $FRONTEND)"
  say "Workspace (links to the two repos ONLY): $WS"
fi

# shellcheck disable=source=missing-file
source "$CONFIG_FILE"
PY="${PY:-python}"

# Self-heal: rebuild the synthetic workspace if its links are missing
# (fresh clone, cleaned ~/.countersign, moved repos + regenerated config).
if [[ -n "${WORKSPACE:-}" ]] && { [[ ! -d "$WORKSPACE/backend" ]] || [[ ! -d "$WORKSPACE/frontend" ]]; }; then
  WORKSPACE=$(build_workspace "$BACKEND" "$FRONTEND" "$PY")
  echo "workspace links rebuilt -> $WORKSPACE"
fi

# --- persisted loop configuration (edit by typing /config at the prompt) ----
RUN_CONFIG_FILE="$SCRIPT_DIR/run-config.sh"
[[ -f "$RUN_CONFIG_FILE" ]] && source "$RUN_CONFIG_FILE"
MAX_IT="${MAX_IT:-4}"
STRATEGY="${STRATEGY:-fresh}"
DO_IMPL="${DO_IMPL:-n}"
EXTRA_ARGS_RAW="${EXTRA_ARGS_RAW:-}"

run_config_prompts() {
  say "Loop configuration (saved to run-config.sh; future runs use it automatically)"
  echo "Press Enter to accept the default shown in brackets."
  echo ""
  ask MAX_IT "Max review->revise iterations" "${MAX_IT:-4}"
  ask STRATEGY "Session strategy (fresh | chained)" "${STRATEGY:-fresh}"
  ask_yn DO_IMPL "Let claude IMPLEMENT after consensus" "${DO_IMPL:-n}"
  read -r -p "Additional orchestrator flags (optional, e.g. --max-retries 0) []: " __extra || true
  EXTRA_ARGS_RAW="${__extra:-}"
  cat > "$RUN_CONFIG_FILE" <<EOF
# Loop configuration (gitignored). Re-run /config in countersign to edit.
MAX_IT=$(printf '%q' "$MAX_IT")
STRATEGY=$(printf '%q' "$STRATEGY")
DO_IMPL=$(printf '%q' "$DO_IMPL")
EXTRA_ARGS_RAW=$(printf '%q' "$EXTRA_ARGS_RAW")
EOF
  echo "Configuration saved."
}

build_args() {
  IMPL_ARGS=()
  [[ "$DO_IMPL" == "y" ]] && IMPL_ARGS=(--implement)
  RULES_ARGS=()
  if [[ -f "$SCRIPT_DIR/agent-review-rules.md" ]]; then
    RULES_ARGS=(--review-rules "$SCRIPT_DIR/agent-review-rules.md")
  fi
  CLI_ARGS=()
  [[ -n "${ZCODE_CLI:-}" ]] && CLI_ARGS+=(--zcode-cli "$ZCODE_CLI")
  [[ -n "${CLAUDE_CLI:-}" ]] && CLI_ARGS+=(--claude-cli "$CLAUDE_CLI")
  EXTRA_ARGS=()
  [[ -n "$EXTRA_ARGS_RAW" ]] && read -r -a EXTRA_ARGS <<< "$EXTRA_ARGS_RAW"
  return 0   # guard: a short-circuited && as the last line would return 1 under set -e
}

python_model_fallback() {
  "$PY" -c "import json,pathlib;p=pathlib.Path.home()/'.zcode/cli/config.json';print('zcode '+json.loads(p.read_text()).get('model',{}).get('main','(model unknown - run preflight)'))" 2>/dev/null || echo "zcode (model unknown)"
}

show_config() {
  say "Current configuration (type /config at the prompt below to change it)"
  echo "  workspace : $WORKSPACE"
  echo "  repos     : backend  = $BACKEND (branch: $(git_branch_of "$BACKEND"))"
  echo "               frontend = $FRONTEND (branch: $(git_branch_of "$FRONTEND"))"
  echo "  agents    : drafter  = ${AGENT_DRAFTER:-claude (model: account default; pin with --model via /config)}"
  echo "               reviewer = ${AGENT_REVIEWER:-$(python_model_fallback)}"
  echo "  loop      : max iterations $MAX_IT | strategy $STRATEGY | implement after consensus: $DO_IMPL"
  local rules="built-in invariants"
  [[ ${#RULES_ARGS[@]} -gt 0 ]] && rules="built-in + agent-review-rules.md"
  echo "  review    : $rules"
  [[ -n "$EXTRA_ARGS_RAW" ]] && echo "  extra     : $EXTRA_ARGS_RAW"
  echo ""
}

# first run (or after deleting run-config.sh): collect loop config once
if [[ ! -f "$RUN_CONFIG_FILE" ]]; then
  run_config_prompts
fi

# --- the task prompt (with /config escape) -----------------------------------
build_args
while :; do
  show_config
  say "YOUR TASK PROMPT - type what the agents should work on"
  echo "   Type /config (Enter) to change settings first."
  echo "   Finish with a blank line (Enter on an empty line submits). Ctrl+C cancels."
  TASK=""
  IFS= read -r line || break
  if [[ "$(printf '%s' "$line" | tr -d '[:space:]')" == "/config" ]]; then
    run_config_prompts
    build_args
    continue
  fi
  TASK+="$line"$'\n'
  while :; do
    IFS= read -r line || break
    if [[ -z "$line" && -n "$TASK" ]]; then break; fi
    TASK+="$line"$'\n'
  done
  if [[ -z "${TASK//[$' \t\n']/}" ]]; then
    echo "Empty task prompt - aborting." >&2
    exit 1
  fi
  break
done

run_orch() { # extra args...
  "$PY" "$ORCH" "$TASK" \
    --repo "$WORKSPACE" \
    --max-iterations "$MAX_IT" \
    --strategy "$STRATEGY" \
    "${IMPL_ARGS[@]}" "${RULES_ARGS[@]}" "${CLI_ARGS[@]}" "${EXTRA_ARGS[@]}" "$@"
}

# --- 3. one-time preflight per device ---------------------------------------
if [[ "${PREFLIGHT_DONE:-no}" != "yes" ]]; then
  say "First run on this device: running preflight (isolated temp dir, cheap calls)"
  PF_OUT=$("$PY" "$ORCH" --preflight x --repo "$WORKSPACE" "${CLI_ARGS[@]}" 2>/dev/null)
  if [[ $? -eq 0 ]]; then
    sed -i.bak 's/^PREFLIGHT_DONE=.*/PREFLIGHT_DONE=yes/' "$CONFIG_FILE" && rm -f "$CONFIG_FILE.bak"
    # persist the detected agent models for the config summary
    AGENT_DRAFTER=$(printf '%s' "$PF_OUT" | "$PY" -c "import json,sys;r=json.load(sys.stdin);print(r.get('agent_models',{}).get('claude',''))" 2>/dev/null)
    AGENT_REVIEWER=$(printf '%s' "$PF_OUT" | "$PY" -c "import json,sys;r=json.load(sys.stdin);print(r.get('agent_models',{}).get('zcode',''))" 2>/dev/null)
    [[ -n "$AGENT_DRAFTER" ]]  && printf 'AGENT_DRAFTER=%q
'  "$AGENT_DRAFTER"  >> "$CONFIG_FILE"
    [[ -n "$AGENT_REVIEWER" ]] && printf 'AGENT_REVIEWER=%q
' "$AGENT_REVIEWER" >> "$CONFIG_FILE"
    say "Preflight OK - will not run again on this device (delete $CONFIG_FILE to redo)"
  else
    ask_yn CONT "Preflight FAILED - run the loop anyway" "n"
    [[ "$CONT" == "n" ]] && exit 2
  fi
fi

# --- 4. launch (with blocked-on-human resume loop) --------------------------
say "Launching dual-agent consensus loop"
exit_code=0
run_orch || exit_code=$?

while [[ "$exit_code" -eq 4 || "$exit_code" -eq 5 ]]; do
  echo ""
  if [[ "$exit_code" -eq 4 ]]; then
    say "Run blocked on human decisions"
    OQ="$WORKSPACE/open-questions.json"
    if [[ -f "$OQ" ]]; then
      echo "Questions are in: $OQ"
      echo "Fill in the \"answer\" fields, save, then press Enter to resume (Ctrl+C to stop)."
      read -r _ || true
      [[ -f "$OQ" ]] || { echo "open-questions.json disappeared - aborting." >&2; exit 4; }
      exit_code=0
      run_orch --decisions "$OQ" || exit_code=$?
    else
      echo "No open-questions.json found at $OQ - aborting." >&2
      exit 4
    fi
  else
    say "Run blocked on branch: an implement target is on main/master"
    echo "Nothing has been edited. Create branches for the repos below (Enter = suggested name):"
    for repo in "$BACKEND" "$FRONTEND"; do
      if [[ "$(git_branch_of "$repo")" == "main" || "$(git_branch_of "$repo")" == "master" ]]; then
        select_impl_branch "$repo" || exit 5
      fi
    done
    echo "Branches ready. Press Enter to re-run (Ctrl+C to stop)."
    read -r _ || true
    exit_code=0
    run_orch || exit_code=$?
  fi
done

echo ""
case "$exit_code" in
  0) say "Consensus reached. Plan: $WORKSPACE/plan.md | History: $WORKSPACE/plan-history" ;;
  3) say "No consensus after $MAX_IT iterations - see $WORKSPACE/plan-history for remaining objections" ;;
  *) say "Run ended with exit code $exit_code (see logs above)" ;;
esac
exit "$exit_code"
