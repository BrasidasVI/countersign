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
CONFIG_FILE="$SCRIPT_DIR/device-config.sh"
ORCH="$SCRIPT_DIR/orchestrator.py"

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
ask()  { # ask VAR "prompt" "default" ; empty answer keeps default
  local __var=$1 __prompt=$2 __def=$3 __ans=""
  read -r -p "$__prompt [$__def]: " __ans || true
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
    printf -v "$__var" '%s' "$__tmp"
  done
}
ask_yn() { # ask_yn VAR "prompt" "y|n default"
  local __var=$1 __prompt=$2 __def=$3 __ans=""
  read -r -p "$__prompt ($([[ $__def == y ]] && echo Y/n || echo y/N)): " __ans || true
  __ans="${__ans:-$__def}"
  [[ "$__ans" == [yY]* ]] && printf -v "$__var" '%s' "y" || printf -v "$__var" '%s' "n"
}

# --- 1. device-local configuration (first run only) ------------------------
if [[ ! -f "$CONFIG_FILE" ]]; then
  say "First run on this device: collecting device-local paths"
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

  # workspace: a directory containing BOTH repos. Default = their common
  # parent; override if that is too broad (e.g. your home directory) or if
  # the repos live in unrelated places.
  WS_GUESS=$("$PY" - "$BACKEND" "$FRONTEND" <<'PYEOF' 2>/dev/null || echo ""
import os, sys
try:
    print(os.path.commonpath([os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])]))
except ValueError:
    pass
PYEOF
)
  if [[ -z "$WS_GUESS" || "$WS_GUESS" == "/" ]]; then
    WS_GUESS="$(dirname "$BACKEND")"
    echo "(Repos do not share a sensible common parent - please give the workspace dir explicitly.)"
  fi
  ask_dir WORKSPACE "Directory containing BOTH repos (agents see everything under it)" "$WS_GUESS"
  while ! "$PY" - "$BACKEND" "$FRONTEND" "$WORKSPACE" <<'PYEOF'
import os, sys
ws = os.path.realpath(sys.argv[3])
ok = all(os.path.realpath(p).startswith(ws.rstrip(os.sep) + os.sep) for p in sys.argv[1:3])
sys.exit(0 if ok else 1)
PYEOF
  do
    echo "One of the repos is not inside the workspace."
    if [[ ! -t 0 ]]; then
      echo "Non-interactive session with an invalid workspace - aborting." >&2
      exit 1
    fi
    ask_dir WORKSPACE "Workspace directory containing BOTH repos" "$WORKSPACE"
  done

  # optional CLI overrides (empty = auto-detect)
  ask ZCODE_CLI "Path to zcode.cjs (blank = auto-detect)" ""
  ask CLAUDE_CLI "Path to claude executable (blank = auto-detect)" ""

  cat > "$CONFIG_FILE" <<EOF
# Device-local launcher config (gitignored). Regenerate by deleting this file.
BACKEND=$(printf '%q' "$BACKEND")
FRONTEND=$(printf '%q' "$FRONTEND")
WORKSPACE=$(printf '%q' "$WORKSPACE")
PY=$(printf '%q' "$PY")
ZCODE_CLI=$(printf '%q' "$ZCODE_CLI")
CLAUDE_CLI=$(printf '%q' "$CLAUDE_CLI")
PREFLIGHT_DONE=no
EOF
  echo ""
  say "Saved device config. (Backend: $BACKEND | Frontend: $FRONTEND)"
fi

# shellcheck disable=source=missing-file
source "$CONFIG_FILE"
PY="${PY:-python}"

# --- 2. per-run configuration ----------------------------------------------
say "Loop configuration"
echo "Press Enter to accept the default shown in brackets."
echo ""
ask MAX_IT "Max review->revise iterations" "4"
ask STRATEGY "Session strategy (fresh | chained)" "fresh"
ask_yn DO_IMPL "Let claude IMPLEMENT after consensus" "n"

IMPL_REPO_ARGS=()
if [[ "$DO_IMPL" == "y" ]]; then
  echo "Which repo does this implementation target?"
  echo "  1) backend  ($BACKEND)"
  echo "  2) frontend ($FRONTEND)"
  ask IMPL_CHOICE "Target (1/2)" "1"
  case "$IMPL_CHOICE" in
    2) IMPL_REPO_ARGS=(--implement-repo "$FRONTEND") ;;
    *) IMPL_REPO_ARGS=(--implement-repo "$BACKEND") ;;
  esac
fi

RULES_ARGS=()
if [[ -f "$SCRIPT_DIR/agent-review-rules.md" ]]; then
  ask_yn USE_RULES "Use agent-review-rules.md review invariants" "y"
  [[ "$USE_RULES" == "y" ]] && RULES_ARGS=(--review-rules "$SCRIPT_DIR/agent-review-rules.md")
fi

read -r -p "Additional orchestrator flags (optional, e.g. --max-retries 0): " EXTRA_ARGS_RAW || true
EXTRA_ARGS=()
[[ -n "$EXTRA_ARGS_RAW" ]] && read -r -a EXTRA_ARGS <<< "$EXTRA_ARGS_RAW"

echo ""
say "Task prompt - describe what the agents should work on."
echo "   Enter a blank line when done. (Ctrl+C cancels.)"
TASK=""
while :; do
  IFS= read -r line || break
  if [[ -z "$line" && -n "$TASK" ]]; then break; fi
  TASK+="$line"$'\n'
done
if [[ -z "${TASK//[$' \t\n']/}" ]]; then
  echo "Empty task prompt - aborting." >&2
  exit 1
fi

CLI_ARGS=()
[[ -n "${ZCODE_CLI:-}" ]] && CLI_ARGS+=(--zcode-cli "$ZCODE_CLI")
[[ -n "${CLAUDE_CLI:-}" ]] && CLI_ARGS+=(--claude-cli "$CLAUDE_CLI")

run_orch() { # extra args...
  "$PY" "$ORCH" "$TASK" \
    --repo "$WORKSPACE" \
    --max-iterations "$MAX_IT" \
    --strategy "$STRATEGY" \
    "${IMPL_REPO_ARGS[@]}" "${RULES_ARGS[@]}" "${CLI_ARGS[@]}" "${EXTRA_ARGS[@]}" "$@"
}

# --- 3. one-time preflight per device ---------------------------------------
if [[ "${PREFLIGHT_DONE:-no}" != "yes" ]]; then
  say "First run on this device: running preflight (isolated temp dir, cheap calls)"
  if "$PY" "$ORCH" --preflight x --repo "$WORKSPACE" "${CLI_ARGS[@]}"; then
    sed -i.bak 's/^PREFLIGHT_DONE=.*/PREFLIGHT_DONE=yes/' "$CONFIG_FILE" && rm -f "$CONFIG_FILE.bak"
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

while [[ "$exit_code" -eq 4 ]]; do
  OQ="$WORKSPACE/open-questions.json"
  echo ""
  say "Run blocked on human decisions"
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
done

echo ""
case "$exit_code" in
  0) say "Consensus reached. Plan: $WORKSPACE/plan.md | History: $WORKSPACE/plan-history" ;;
  3) say "No consensus after $MAX_IT iterations - see $WORKSPACE/plan-history for remaining objections" ;;
  *) say "Run ended with exit code $exit_code (see logs above)" ;;
esac
exit "$exit_code"
