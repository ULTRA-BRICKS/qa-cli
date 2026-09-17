"""Browser driver for Telegram Web via the agent-browser CLI.

Accessed via: `qa telegram login|status` -> AgentBrowserDriver (production)
or an injected BrowserDriver (hermetic tests).

Assumptions: default binary is `agent-browser` on PATH (override QA_BROWSER_BIN);
headless Chromium is agent-browser's default (no --headed); session name
qa-telegram with --restore so re-runs skip QR. QR-ready means a painted
QR-related canvas (non-uniform opaque pixels), a QR-labeled img, or a
QR-related svg (>= 80px) — a blank or unrelated canvas is not ready.
wait_for_qr also returns if a restored
session becomes logged-in, so re-runs do not wait for a QR that never appears.

Telegram Web A (web.telegram.org/a/) is a TDLib client, not the React "Web K"
client: it has no #LeftColumn/#column-left at all. Auth state is detected via
literal (non-hashed) markup Telegram itself emits: every pre-login screen
renders an `#auth-<step>-form` element (`auth-qr-form`, `auth-phone-number-form`,
and — by the same convention — `auth-password-form` for the 2FA/cloud-password
step) inside a `.is-auth` wrapper that Telegram removes the moment auth
finishes. auth_status() reports the exact form id so a stuck login (e.g.
scanned on the phone but blocked on a cloud password this CLI never types)
is diagnosable instead of silently retrying until timeout.

get_qr_token() extracts the live QR payload (the `tg://login?token=...`
string) by dynamically re-importing Telegram's own `qr-code-styling` bundle
— found by grepping the page's entry script for the literal
"qr-code-styling" specifier, so it survives Vite's per-build content
hashes — and monkeypatching its shared `QRCodeStyling.prototype.update` to
record the `data` argument Telegram passes on every render/rotation
(~every 20-30s). This reads the same third-party library call Telegram's own
UI makes, not a screenshot/OCR guess.

Never invokes macOS `open -a Telegram`, Playwright, Puppeteer, or npx.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import NamedTuple, Optional, Protocol

TELEGRAM_WEB_URL = "https://web.telegram.org/a/"
SESSION_NAME = "qa-telegram"
INSTALL_HINT = "npm i -g agent-browser && agent-browser install"


class AuthStatus(NamedTuple):
    """Result of probing Telegram Web A's current authorization state."""

    logged_in: bool
    # DOM id of the current `#auth-<step>-form`, e.g. "auth-qr-form" or
    # "auth-password-form"; None once logged in or before any form renders.
    waiting_form: Optional[str]


# Side-effect free: read only. Telegram Web A renders every pre-login screen
# as `#auth-<step>-form` inside a `.is-auth` wrapper — both are literal
# strings in Telegram's source, not Vite/CSS-module hashes, so they survive
# rebuilds. `.is-auth` is removed from the DOM entirely once auth finishes.
_AUTH_STATUS_JS = (
    "(() => {"
    "  const form = document.querySelector('[id^=\"auth-\"][id$=\"-form\"]');"
    "  if (form) return JSON.stringify({ loggedIn: false, waitingForm: form.id });"
    "  if (document.querySelector('.is-auth')) {"
    "    return JSON.stringify({ loggedIn: false, waitingForm: null });"
    "  }"
    "  return JSON.stringify({ loggedIn: true, waitingForm: null });"
    "})()"
)

# Side-effect free: read only. Finds Telegram's own dynamically-imported
# qr-code-styling bundle by grepping the entry module's source for the
# literal specifier (stable across Vite content-hash rebuilds), then
# monkeypatches the shared QRCodeStyling.prototype.update so every future
# render/rotation records its `data` (a `tg://login?token=...` string) on
# window.__qaQrHook. Idempotent: a second call reuses the existing hook.
_QR_TOKEN_JS = (
    "(async () => {"
    "  const w = window;"
    "  if (!w.__qaQrHook) {"
    "    w.__qaQrHook = { token: null };"
    "    try {"
    "      const mainScript = Array.from("
    "        document.querySelectorAll('script[type=\"module\"][src]')"
    "      ).map((s) => s.src)[0];"
    "      if (!mainScript) throw new Error('no entry module script found');"
    "      const src = await (await fetch(mainScript)).text();"
    "      const match = src.match(/[\"']([^\"']*qr-code-styling[^\"']*\\.js)[\"']/);"
    "      if (!match) throw new Error('qr-code-styling specifier not found');"
    "      const qrUrl = new URL(match[1], mainScript).href;"
    "      const mod = await import(qrUrl);"
    "      const proto = mod && mod.default && mod.default.prototype;"
    "      if (!proto || typeof proto.update !== 'function') {"
    "        throw new Error('unexpected qr-code-styling module shape');"
    "      }"
    "      const orig = proto.update;"
    "      const prefix = 'tg://login?token=';"
    "      proto.update = function (opts) {"
    "        try {"
    "          if (opts && typeof opts.data === 'string' && opts.data.indexOf(prefix) === 0) {"
    "            w.__qaQrHook.token = opts.data.slice(prefix.length);"
    "          }"
    "        } catch (e) {}"
    "        return orig.apply(this, arguments);"
    "      };"
    "    } catch (e) {"
    "      w.__qaQrHook.error = String((e && e.message) || e);"
    "    }"
    "  }"
    "  return JSON.stringify(w.__qaQrHook);"
    "})()"
)

# Side-effect free: read only. Compact page state for a timeout error so a
# stuck login is debuggable from CLI output alone, without reopening a
# headed browser.
_DIAGNOSTIC_JS = (
    "(() => {"
    "  const text = document.body ? (document.body.innerText || '') : '';"
    "  return JSON.stringify({"
    "    title: document.title,"
    "    url: location.href,"
    "    text: text.replace(/\\s+/g, ' ').trim().slice(0, 300),"
    "  });"
    "})()"
)

# Side-effect free: painted QR canvas, QR <img>, or QR-related <svg>.
# Telegram sizes a canvas before drawing; dimensions alone are not ready.
# Live Web A currently paints the login QR as SVG inside .qr-container.
_QR_VISIBLE_JS = (
    "(() => {"
    "  const qrHay = (el) => {"
    "    const cls = typeof el.className === 'string' ? el.className"
    "      : ((el.className && el.className.baseVal) || '');"
    "    const aria = el.getAttribute ? (el.getAttribute('aria-label') || '') : '';"
    "    return (cls + ' ' + (el.id || '') + ' ' + (el.alt || '') + ' '"
    "      + (el.src || '') + ' ' + aria).toLowerCase();"
    "  };"
    "  const isQrRelated = (el) => {"
    "    let n = el;"
    "    for (let i = 0; i < 8 && n; i++) {"
    "      if (/qr/.test(qrHay(n))) return true;"
    "      n = n.parentElement;"
    "    }"
    "    return false;"
    "  };"
    "  const isPainted = (c) => {"
    "    try {"
    "      const ctx = c.getContext && c.getContext('2d');"
    "      if (!ctx || !ctx.getImageData) return false;"
    "      const w = c.width || 0, h = c.height || 0;"
    "      if (w < 1 || h < 1) return false;"
    "      const d = ctx.getImageData(0, 0, w, h).data;"
    "      let first = -1;"
    "      for (let i = 0; i < d.length; i += 4) {"
    "        if (d[i + 3] < 16) continue;"
    "        const key = (d[i] << 16) | (d[i + 1] << 8) | d[i + 2];"
    "        if (first < 0) first = key;"
    "        else if (key !== first) return true;"
    "      }"
    "      return false;"
    "    } catch (e) { return false; }"
    "  };"
    "  const svgSize = (s) => {"
    "    const attrW = parseFloat(s.getAttribute && s.getAttribute('width')) || 0;"
    "    const attrH = parseFloat(s.getAttribute && s.getAttribute('height')) || 0;"
    "    const baseW = (s.width && s.width.baseVal && s.width.baseVal.value) || attrW;"
    "    const baseH = (s.height && s.height.baseVal && s.height.baseVal.value) || attrH;"
    "    let vbW = 0, vbH = 0;"
    "    const vb = s.getAttribute && s.getAttribute('viewBox');"
    "    if (vb) {"
    "      const p = vb.trim().split(/\\s+/);"
    "      if (p.length === 4) { vbW = parseFloat(p[2]) || 0; vbH = parseFloat(p[3]) || 0; }"
    "    }"
    "    return {"
    "      w: Math.max(baseW, vbW, s.clientWidth || 0),"
    "      h: Math.max(baseH, vbH, s.clientHeight || 0)"
    "    };"
    "  };"
    "  const qrSvg = document.querySelector('.qr-container svg');"
    "  if (qrSvg) {"
    "    const { w, h } = svgSize(qrSvg);"
    "    if (w >= 80 && h >= 80) return 'qr-visible';"
    "  }"
    "  const canvases = Array.from(document.querySelectorAll('canvas'));"
    "  for (const c of canvases) {"
    "    const w = Math.max(c.width || 0, c.clientWidth || 0);"
    "    const h = Math.max(c.height || 0, c.clientHeight || 0);"
    "    if (w >= 80 && h >= 80 && isQrRelated(c) && isPainted(c)) return 'qr-visible';"
    "  }"
    "  const imgs = Array.from(document.querySelectorAll('img'));"
    "  for (const img of imgs) {"
    "    const w = Math.max(img.naturalWidth || 0, img.clientWidth || 0);"
    "    const h = Math.max(img.naturalHeight || 0, img.clientHeight || 0);"
    "    if (isQrRelated(img) && w >= 80 && h >= 80) return 'qr-visible';"
    "  }"
    "  const svgs = Array.from(document.querySelectorAll('svg'));"
    "  for (const s of svgs) {"
    "    const { w, h } = svgSize(s);"
    "    if (isQrRelated(s) && w >= 80 && h >= 80) return 'qr-visible';"
    "  }"
    "  return 'qr-missing';"
    "})()"
)


class BrowserError(RuntimeError):
    """agent-browser ran but failed (nonzero exit)."""


class BrowserMissingError(FileNotFoundError):
    """Raised when the agent-browser binary is not on PATH."""

    def __init__(self, binary: str) -> None:
        super().__init__(binary)
        self.binary = binary


class BrowserDriver(Protocol):
    def open(self, url: str) -> None: ...
    def screenshot(self, path: str) -> None: ...
    def is_logged_in(self) -> bool: ...
    def wait_for_qr(self, timeout: float) -> None: ...
    def auth_status(self) -> AuthStatus: ...
    def get_qr_token(self) -> Optional[str]: ...
    def diagnostic_dump(self) -> str: ...


def browser_bin() -> str:
    return os.environ.get("QA_BROWSER_BIN") or "agent-browser"


class AgentBrowserDriver:
    """Drives Telegram Web through the agent-browser CLI (stdlib subprocess)."""

    def __init__(self, binary: Optional[str] = None) -> None:
        self.binary = binary or browser_bin()

    def build_argv(self, *args: str) -> list[str]:
        # Explicit restore key so the `open` subcommand is never parsed as the
        # optional --restore name.
        return [
            self.binary,
            "--session",
            SESSION_NAME,
            "--restore",
            SESSION_NAME,
            *args,
        ]

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = self.build_argv(*args)
        try:
            proc = subprocess.run(argv, check=False, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise BrowserMissingError(self.binary) from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise BrowserError(
                f"{self.binary} exited {proc.returncode}"
                + (f": {detail}" if detail else "")
            )
        return proc

    def open(self, url: str) -> None:
        self._run("open", url)

    def screenshot(self, path: str) -> None:
        self._run("screenshot", path)

    def _eval_json(self, js: str):
        """Run `js` and decode agent-browser's JSON.stringify(...) return value.

        agent-browser prints a JS string return value JSON-encoded, so a
        `JSON.stringify(...)` payload arrives double-encoded (once from
        agent-browser's own quoting, once from our script).
        """
        proc = self._run("eval", js)
        raw = (proc.stdout or "").strip()
        try:
            outer = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(outer, str):
            return outer
        try:
            return json.loads(outer)
        except (json.JSONDecodeError, ValueError):
            return None

    def auth_status(self) -> AuthStatus:
        data = self._eval_json(_AUTH_STATUS_JS)
        if not isinstance(data, dict):
            return AuthStatus(logged_in=False, waiting_form=None)
        waiting_form = data.get("waitingForm")
        return AuthStatus(
            logged_in=bool(data.get("loggedIn")),
            waiting_form=waiting_form if isinstance(waiting_form, str) else None,
        )

    def is_logged_in(self) -> bool:
        return self.auth_status().logged_in

    def get_qr_token(self) -> Optional[str]:
        data = self._eval_json(_QR_TOKEN_JS)
        if not isinstance(data, dict):
            return None
        token = data.get("token")
        return token if isinstance(token, str) else None

    def diagnostic_dump(self) -> str:
        data = self._eval_json(_DIAGNOSTIC_JS)
        if not isinstance(data, dict):
            return "diagnostic unavailable (eval failed)"
        return (
            f"title={data.get('title', '')!r} "
            f"url={data.get('url', '')!r} "
            f"text={data.get('text', '')!r}"
        )

    def is_qr_visible(self) -> bool:
        proc = self._run("eval", _QR_VISIBLE_JS)
        out = (proc.stdout or "").strip().strip('"').strip("'")
        return out.endswith("qr-visible") or out == "qr-visible"

    def wait_for_qr(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_logged_in() or self.is_qr_visible():
                return
            time.sleep(0.5)
        raise TimeoutError("timed out waiting for Telegram Web QR code")
