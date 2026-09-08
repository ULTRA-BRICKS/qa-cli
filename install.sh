#!/usr/bin/env bash
# install.sh — install the `qa` CLI (Python 3)
# Works both from a local clone (./install.sh) and piped from curl:
#   curl -fsSL https://raw.githubusercontent.com/alex-mextner/qa-cli/main/install.sh | bash
#
# qa-cli has ZERO runtime Python deps. This installer is PIPX-FIRST (isolated
# venv + `qa` on PATH). Without pipx it falls back to a symlink of bin/qa.
set -euo pipefail

# ── identity ──────────────────────────────────────────────────────────────────
TOOL="qa"
REPO="qa-cli"
GITHUB_USER="alex-mextner"
ENTRY="bin/qa"     # path inside repo root
CLONE_BASE="${XDG_DATA_HOME:-$HOME/.local/share}"

# ── locate source dir ─────────────────────────────────────────────────────────
_script_dir=""
if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" ]]; then
  _script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$_script_dir" && -f "$_script_dir/$ENTRY" ]]; then
  SRC="$_script_dir"
  echo "qa: using local clone at $SRC"
else
  mkdir -p "$CLONE_BASE"
  CLONE_DIR="$CLONE_BASE/$REPO"
  EXPECT_URL="https://github.com/$GITHUB_USER/$REPO.git"
  if [[ -d "$CLONE_DIR/.git" ]]; then
    actual_url="$(git -C "$CLONE_DIR" remote get-url origin 2>/dev/null || echo "")"
    if [[ "$actual_url" != "$EXPECT_URL" ]]; then
      echo "ERROR: $CLONE_DIR exists but its origin is '$actual_url', not $EXPECT_URL." >&2
      echo "       Remove that directory or fix its remote, then re-run." >&2
      exit 1
    fi
    echo "qa: updating existing clone at $CLONE_DIR"
    git -C "$CLONE_DIR" pull --ff-only
  else
    echo "qa: cloning $EXPECT_URL into $CLONE_DIR"
    git clone "$EXPECT_URL" "$CLONE_DIR"
  fi
  SRC="$CLONE_DIR"
fi

# ── bin dir ───────────────────────────────────────────────────────────────────
BIN="${PIPX_BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN"

if [[ ":$PATH:" != *":$BIN:"* ]]; then
  echo ""
  echo "  NOTE: $BIN is not on your PATH."
  echo "  Add the following line to your ~/.bashrc or ~/.zshrc and restart your shell:"
  echo "    export PATH=\"$BIN:\$PATH\""
  echo ""
fi

# ── install ───────────────────────────────────────────────────────────────────
QA_BIN=""
INSTALL_MODE=""
if command -v pipx >/dev/null 2>&1; then
  INSTALL_MODE="pipx"
  echo "qa: installing via pipx (isolated venv; zero runtime deps)"
  pipx install --force "$SRC"
  if [[ -x "$BIN/$TOOL" ]]; then
    QA_BIN="$BIN/$TOOL"
  else
    QA_BIN="$(command -v "$TOOL" 2>/dev/null || true)"
  fi
  if [[ -z "$QA_BIN" || ! -x "$QA_BIN" ]]; then
    echo "  ERROR: pipx install succeeded but '$TOOL' is not on PATH (check $BIN / PIPX_BIN_DIR)" >&2
    exit 1
  fi
  echo "qa: pipx installed $TOOL at $QA_BIN"
else
  INSTALL_MODE="symlink"
  echo ""
  echo "  NOTE: pipx not found — falling back to a symlink."
  echo "  For a clean ISOLATED install (recommended), install pipx and re-run:"
  echo "    python3 -m pip install --user pipx && python3 -m pipx ensurepath"
  echo ""
  ENTRY_PATH="$SRC/$ENTRY"
  chmod +x "$ENTRY_PATH"
  ln -sfn "$ENTRY_PATH" "$BIN/$TOOL"
  QA_BIN="$BIN/$TOOL"
  echo "qa: symlinked $BIN/$TOOL -> $ENTRY_PATH"
fi

# ── shadow check ──────────────────────────────────────────────────────────────
RESOLVED="$(command -v "$TOOL" 2>/dev/null || true)"
if [[ -n "$RESOLVED" && "$RESOLVED" != "$QA_BIN" ]]; then
  echo ""
  echo "  WARNING: another '$TOOL' shadows our install on PATH:" >&2
  echo "      installed: $QA_BIN" >&2
  echo "      resolves to: $RESOLVED" >&2
  echo "  Ensure the dir holding our install precedes the other on PATH, or remove the other." >&2
  echo ""
fi

# ── done ──────────────────────────────────────────────────────────────────────
PATH_RESOLVED="$(command -v "$TOOL" 2>/dev/null || true)"
if [[ -z "$PATH_RESOLVED" ]]; then
  echo "" >&2
  echo "  WARNING: $TOOL is installed at $QA_BIN, but does NOT resolve by name on PATH." >&2
  echo "           ($BIN is not on your PATH, so the bare '$TOOL' command will not work yet.)" >&2
  echo "  Fix it:  add $BIN to PATH, then restart your shell:" >&2
  echo "             export PATH=\"$BIN:\$PATH\"" >&2
  echo "  Until then, run $TOOL by full path: $QA_BIN" >&2
  echo "" >&2
  exit 1
fi

echo ""
echo "  qa is installed (via $INSTALL_MODE)."
echo "  Usage: qa telegram login    — save QR to ~/.config/qa-cli/telegram-qr.png"
echo "         qa telegram status   — logged-in / logged-out"
echo "         qa --help            — full usage"
echo "  Tip:   pipx is the primary path — 'pipx install git+https://github.com/$GITHUB_USER/$REPO'"
echo ""
