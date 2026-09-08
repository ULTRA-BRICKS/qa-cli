"""Telegram Web QR login and session status.

Accessed via: `qa telegram login` and `qa telegram status` (qa_cli.cli.main).

Assumptions: a BrowserDriver is provided (AgentBrowserDriver in production,
a fake in hermetic tests). Config dir is ~/.config/qa-cli unless QA_CONFIG_DIR
is set. Never launches Telegram.app and never sends chat messages.
"""
from __future__ import annotations

import os
from pathlib import Path

from qa_cli.browser import SESSION_NAME, TELEGRAM_WEB_URL, BrowserDriver

DEFAULT_LOGIN_TIMEOUT = 300.0


def config_dir() -> Path:
    override = os.environ.get("QA_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "qa-cli"


def qr_path() -> Path:
    return config_dir() / "telegram-qr.png"


def login(browser: BrowserDriver, timeout: float = DEFAULT_LOGIN_TIMEOUT) -> int:
    config_dir().mkdir(parents=True, exist_ok=True)
    browser.open(TELEGRAM_WEB_URL)
    if browser.is_logged_in():
        print("logged-in")
        print(f"session: {SESSION_NAME}")
        print(f"config: {config_dir()}")
        return 0
    dest = qr_path()
    browser.screenshot(str(dest))
    print(str(dest))
    browser.wait_until_logged_in(timeout)
    return 0


def status(browser: BrowserDriver) -> int:
    browser.open(TELEGRAM_WEB_URL)
    state = "logged-in" if browser.is_logged_in() else "logged-out"
    print(state)
    print(f"session: {SESSION_NAME}")
    print(f"config: {config_dir()}")
    return 0
