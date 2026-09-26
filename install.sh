#!/usr/bin/env bash
# install.sh — install the `qa` CLI (Python 3)
# Works both from a local clone (./install.sh) and piped from curl:
#   curl -fsSL https://git.hyperide.ai/ultrabricks/qa-cli/raw/branch/main/install.sh | bash
#
# qa-cli has ZERO runtime Python deps. This installer is PIPX-FIRST (isolated
# venv + `qa` on PATH). Without pipx it falls back to a symlink of bin/qa.
set -euo pipefail

# ── identity ──────────────────────────────────────────────────────────────────
TOOL="qa"
REPO="qa-cli"
FORGE="https://git.hyperide.ai/ultrabricks"   # canonical home; github.com/alex-mextner is an archived mirror
ENTRY="bin/qa"     # path inside repo root
CLONE_BASE="${XDG_DATA_HOME:-$HOME/.local/share}"
EXPECT_URL="$FORGE/$REPO.git"
DIST="hyper-qa"   # PyPI distribution name

# ── Windows (Cygwin / MSYS2 / Git Bash) ───────────────────────────────────────
# There python, git and uv are native Windows programs: a symlink to the entry script (a
# `#!/usr/bin/env python3` file) cannot run, and Cygwin's own ~/.local/bin is not the one on
# PATH. `uv tool install` puts a real <tool>.exe into %USERPROFILE%\.local\bin, which IS on PATH
# in every Windows shell — so on Windows that is the whole install.
case "$(uname -s)" in
  CYGWIN*|MINGW*|MSYS*)
    if ! command -v uv >/dev/null 2>&1; then
      echo "ERROR: on Windows $TOOL installs through uv, and uv is not on PATH." >&2
      echo "       Install it:  winget install astral-sh.uv   (or: powershell -c \"irm https://astral.sh/uv/install.ps1 | iex\")" >&2
      exit 1
    fi
    _with=(); [[ -n "${UV_WITH:-}" ]] && _with=(--with "$UV_WITH")  # one argument, spaces and all
    _here=""
    if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" ]]; then
      _here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi
    if [[ -n "$_here" && -f "$_here/$ENTRY" ]]; then
      _src="$_here"; command -v cygpath >/dev/null 2>&1 && _src="$(cygpath -w "$_here")"
      echo "$TOOL: Windows — uv tool install --editable $_src"
      uv tool install --force --editable "$_src" ${_with[@]+"${_with[@]}"}
    else
      echo "$TOOL: Windows — uv tool install git+${EXPECT_URL%.git}"
      uv tool install --force "git+${EXPECT_URL%.git}" ${_with[@]+"${_with[@]}"}
    fi
    _bindir="$(uv tool dir --bin 2>/dev/null || true)"
    if [[ -n "$_bindir" ]] && command -v cygpath >/dev/null 2>&1; then _bindir="$(cygpath -u "$_bindir")"; fi
    if [[ -n "$_bindir" && ":$PATH:" != *":$_bindir:"* ]]; then
      echo ""
      echo "  NOTE: $_bindir (where uv put $TOOL.exe) is not on PATH — run 'uv tool update-shell'"
      echo "        or add it to PATH, then open a new shell."
    fi
    echo ""
    if [[ -n "${_src:-}" ]]; then _upd="git pull in $_here"; else _upd="uv tool upgrade $DIST"; fi
    echo "  $TOOL is installed (uv tool). Update: $_upd"
    exit 0
    ;;
esac

# Origins that are the same repo: the canonical Forgejo one and the pre-migration GitHub ones (a
# GitHub origin is re-pointed at Forgejo — the GitHub copy is frozen). Compared as HOST + PATH, never
# as a substring: an origin that merely contains "hyperide.ai" (git.hyperide.ai.evil.example,
# evil.example/git.hyperide.ai/…) must not pass for the canonical repo and get pulled + installed.
_url_host_path() {  # https / ssh / scp-style git URL -> "host path" (user@, :port, .git, trailing / dropped)
  local _u _hp _path _host
  _u=${1%/}; _u=${_u%.git}
  case "$_u" in
    https://*|ssh://*) _u=${_u#*://}; _hp=${_u%%/*}; _path=${_u#*/} ;;
    *@*:*) _hp=${_u%%:*}; _path=${_u#*:} ;;
    *) return 1 ;;
  esac
  [ "$_path" != "$_u" ] || return 1
  case "$_hp" in *[!A-Za-z0-9._:@%~-]*) return 1 ;; esac  # e.g. `\` (a host separator to some parsers)
  _host=${_hp##*@}; _host=${_host%%:*}
  printf '%s %s\n' "$_host" "$_path"
}
_origin_is_canonical() {
  [ "$(_url_host_path "$1")" = "git.hyperide.ai ultrabricks/$REPO" ]
}
_origin_is_legacy() {
  case "$(_url_host_path "$1")" in
    "github.com alex-mextner/$REPO"|"github.com ultrabricks/$REPO") return 0 ;;
  esac
  return 1
}

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
  if [[ -d "$CLONE_DIR/.git" ]]; then
    actual_url="$(git -C "$CLONE_DIR" remote get-url origin 2>/dev/null || echo "")"
    if _origin_is_legacy "$actual_url"; then
      echo "qa: $REPO moved to $FORGE — re-pointing origin ($actual_url -> $EXPECT_URL)"
      git -C "$CLONE_DIR" remote set-url origin "$EXPECT_URL"
    elif ! _origin_is_canonical "$actual_url"; then
      echo "ERROR: $CLONE_DIR exists but its origin is '$actual_url', not $EXPECT_URL." >&2
      echo "       Remove that directory or fix its remote, then re-run." >&2
      exit 1
    fi
    echo "qa: updating existing clone at $CLONE_DIR"
    # The validated origin (not whatever branch.*.remote names) and the branch that is checked out
    # (a clone kept on a feature branch updates that branch; a detached HEAD takes origin HEAD).
    _branch="$(git -C "$CLONE_DIR" symbolic-ref --quiet --short HEAD || echo HEAD)"
    git -C "$CLONE_DIR" pull --ff-only origin "$_branch"
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
  pipx install --force --editable "$SRC"
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
echo "  Tip:   pipx is the primary path — 'pipx install git+$FORGE/$REPO'"
echo ""
