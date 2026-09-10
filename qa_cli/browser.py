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
Never invokes macOS `open -a Telegram`, Playwright, Puppeteer, or npx.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Optional, Protocol

TELEGRAM_WEB_URL = "https://web.telegram.org/a/"
SESSION_NAME = "qa-telegram"
INSTALL_HINT = "npm i -g agent-browser && agent-browser install"

# Telegram Web A (web.telegram.org/a/) shows a QR (SVG in .qr-container,
# sometimes canvas/img) before login and #LeftColumn afterwards.
# Keep the probe side-effect free: no clicks, no sends.
_LOGGED_IN_JS = (
    "(() => {"
    "  const text = document.body ? (document.body.innerText || '') : '';"
    "  if (/Log in to Telegram by QR/i.test(text)) return 'logged-out';"
    "  if (document.querySelector('#LeftColumn, #column-left')) return 'logged-in';"
    "  if (/Saved Messages|Archived chats/i.test(text)) return 'logged-in';"
    "  return 'logged-out';"
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
    def wait_until_logged_in(self, timeout: float) -> None: ...


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

    def is_logged_in(self) -> bool:
        proc = self._run("eval", _LOGGED_IN_JS)
        out = (proc.stdout or "").strip().strip('"').strip("'")
        return out.endswith("logged-in") or out == "logged-in"

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

    def wait_until_logged_in(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_logged_in():
                return
            time.sleep(2.0)
        raise TimeoutError("timed out waiting for Telegram Web QR login")
