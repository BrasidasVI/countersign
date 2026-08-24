#!/usr/bin/env bash
# install.sh — install the global `countersign` command on this device.
#
# Per-device: run once after cloning (or after moving the repo / git pull if
# the repo moved). Creates shims that point back at THIS clone. Idempotent.
# Uninstall: rm ~/.local/bin/countersign (and countersign.cmd on Windows).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"

case "$(uname -s)" in
  Linux*|Darwin*) OS=unix ;;
  MINGW*|MSYS*|CYGWIN*) OS=windows ;;
  *) echo "Unrecognized platform: $(uname -s)" >&2; exit 1 ;;
esac

mkdir -p "$BIN_DIR"

# --- bash shim (all platforms; Git Bash on Windows) -------------------------
cat > "$BIN_DIR/countersign" <<SHIM
#!/usr/bin/env bash
# Installed by countersign install.sh — repo at: $SCRIPT_DIR
exec bash "$SCRIPT_DIR/launch.sh" "\$@"
SHIM
chmod +x "$BIN_DIR/countersign"

# --- cmd/PowerShell shim (Windows only) --------------------------------------
if [[ "$OS" == "windows" ]]; then
  # Embed the ABSOLUTE bash path: plain 'bash' is not on the PATH of a clean
  # PowerShell/cmd session, and the shim must work there.
  BASH_EXE=$(cygpath -w "$(command -v bash)" 2>/dev/null || echo "bash")
  cat > "$BIN_DIR/countersign.cmd" <<SHIM
@echo off
rem Installed by countersign install.sh — repo at: $SCRIPT_DIR
"$BASH_EXE" "$SCRIPT_DIR/launch.sh" %*
SHIM
fi

# --- make sure $BIN_DIR is on PATH ------------------------------------------
on_path=0
case ":$PATH:" in *":$BIN_DIR:"*) on_path=1 ;; esac

if [[ $on_path -eq 0 ]]; then
  # bash shells (both OSes)
  if ! grep -qs '\.local/bin' "$HOME/.bashrc" 2>/dev/null; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    echo "Added ~/.local/bin to PATH via ~/.bashrc"
  fi
  if [[ "$OS" == "windows" ]]; then
    # Windows user PATH for PowerShell/cmd (safe API method; no setx truncation)
    WINDIR=$(cygpath -w "$BIN_DIR" 2>/dev/null || echo "$BIN_DIR")
    if powershell.exe -NoProfile -Command \
      "[Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';$WINDIR', 'User')" 2>/dev/null; then
      echo "Added $WINDIR to the Windows user PATH (applies to new terminals)."
    else
      echo "NOTE: could not update the Windows PATH automatically."
      echo "      Add $WINDIR to PATH manually for PowerShell/cmd usage."
    fi
  else
    echo "Ubuntu adds ~/.local/bin to PATH at login now that it exists;"
    echo "open a new terminal (or: source ~/.profile) for 'countersign'."
  fi
fi

echo ""
echo "Installed: countersign  ->  $SCRIPT_DIR/launch.sh"
echo "Prerequisites (verified on first run, with install instructions on failure):"
echo "  - Claude Code  (https://claude.ai/code)"
echo "  - ZCode        (https://z.ai — desktop app; its bundled CLI is invoked)"
echo "Run 'countersign' from any directory."
