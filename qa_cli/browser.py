"""Browser driver for Telegram Web via the agent-browser CLI.

Accessed via: `qa telegram login|status` -> AgentBrowserDriver (production)
or an injected BrowserDriver (hermetic tests).

Assumptions: default binary is `agent-browser` on PATH (override QA_BROWSER_BIN);
headless Chromium is agent-browser's default (no --headed); session name
qa-telegram with --restore so re-runs skip QR. Never invokes macOS
`open -a Telegram`, Playwright, Puppeteer, or npx.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Optional, Protocol

TELEGRAM_WEB_URL = "https://web.telegram.org/a/"
SESSION_NAME = "qa-telegram"
INSTALL_HINT = "npm i -g agent-browser && agent-browser install"

# Telegram Web A (web.telegram.org/a/) shows a QR canvas before login and
# #LeftColumn afterwards. Keep the probe side-effect free: no clicks, no sends.
_LOGGED_IN_JS = (
    "(() => {"
    "  const text = document.body ? (document.body.innerText || '') : '';"
    "  if (/Log in to Telegram by QR/i.test(text)) return 'logged-out';"
    "  if (document.querySelector('#LeftColumn, #column-left')) return 'logged-in';"
    "  if (/Saved Messages|Archived chats/i.test(text)) return 'logged-in';"
    "  return 'logged-out';"
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

    def wait_until_logged_in(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_logged_in():
                return
            time.sleep(2.0)
        raise TimeoutError("timed out waiting for Telegram Web QR login")
