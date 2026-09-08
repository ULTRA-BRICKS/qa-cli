"""Hermetic tests for `qa telegram login` / `qa telegram status`.

Accessed via: `pytest tests/test_telegram.py` (CI and local). Default pytest
injects FakeBrowserDriver — never Chromium, never web.telegram.org, never
Telegram.app. Live agent-browser is opt-in via QA_TELEGRAM_LIVE=1.

Assumptions: production `qa_cli.cli.main` is the unit under test. The fake
browser is only a boundary stub (records calls, writes a tiny PNG). Tests
never send chat messages.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from qa_cli.browser import AgentBrowserDriver  # noqa: E402
from qa_cli.cli import main  # noqa: E402

TELEGRAM_WEB = "https://web.telegram.org/a/"

# 1x1 transparent PNG so screenshot() produces a real image file.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

FORBIDDEN_ARGV = ("-a", "Telegram", "Telegram.app", "playwright", "puppeteer", "npx")


class FakeBrowserDriver:
    """Test double: records calls, never shells out."""

    def __init__(self, logged_in: bool = False) -> None:
        self.logged_in = logged_in
        self.calls: list[tuple] = []

    def open(self, url: str) -> None:
        self.calls.append(("open", url))

    def screenshot(self, path: str) -> None:
        self.calls.append(("screenshot", path))
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_TINY_PNG)

    def is_logged_in(self) -> bool:
        self.calls.append(("is_logged_in",))
        return self.logged_in

    def wait_until_logged_in(self, timeout: float) -> None:
        self.calls.append(("wait_until_logged_in", timeout))
        self.logged_in = True


def test_help_lists_telegram_login(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--help"])
    assert code == 0
    out = capsys.readouterr().out
    assert "telegram" in out
    assert "login" in out


def test_telegram_help_documents_qr_path(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["telegram", "--help"])
    assert code == 0
    out = capsys.readouterr().out
    assert "login" in out
    assert "status" in out
    assert "telegram-qr.png" in out


def test_login_help_documents_qr_path(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["telegram", "login", "--help"])
    assert code == 0
    out = capsys.readouterr().out
    assert "telegram-qr.png" in out


def test_login_saves_qr_when_logged_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    fake = FakeBrowserDriver(logged_in=False)
    code = main(["telegram", "login"], browser=fake)
    assert code == 0
    qr = tmp_path / "telegram-qr.png"
    assert qr.is_file()
    assert qr.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    out = capsys.readouterr().out
    assert str(qr) in out
    assert fake.calls[0] == ("open", TELEGRAM_WEB)
    assert any(c[0] == "screenshot" for c in fake.calls)
    assert any(c[0] == "wait_until_logged_in" for c in fake.calls)


def test_login_skips_qr_when_already_logged_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    fake = FakeBrowserDriver(logged_in=True)
    code = main(["telegram", "login"], browser=fake)
    assert code == 0
    assert not (tmp_path / "telegram-qr.png").exists()
    out = capsys.readouterr().out
    assert "logged-in" in out
    assert not any(c[0] == "screenshot" for c in fake.calls)
    assert not any(c[0] == "wait_until_logged_in" for c in fake.calls)


def test_agent_browser_argv_uses_agent_browser_not_telegram_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QA_BROWSER_BIN", raising=False)
    driver = AgentBrowserDriver()
    argv = driver.build_argv("open", TELEGRAM_WEB)
    assert argv[0] == "agent-browser"
    assert TELEGRAM_WEB in argv
    assert argv[0] != "open"
    assert "open -a" not in " ".join(argv)
    for tok in FORBIDDEN_ARGV:
        assert tok not in argv


def test_qa_browser_bin_overrides_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_BROWSER_BIN", "my-agent-browser")
    driver = AgentBrowserDriver()
    argv = driver.build_argv("open", TELEGRAM_WEB)
    assert argv[0] == "my-agent-browser"
    assert TELEGRAM_WEB in argv
    for tok in FORBIDDEN_ARGV:
        assert tok not in argv


def test_status_logged_out_without_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    fake = FakeBrowserDriver(logged_in=False)
    code = main(["telegram", "status"], browser=fake)
    assert code == 0
    out = capsys.readouterr().out
    assert "logged-out" in out
    assert "qa-telegram" in out
    assert not any(c[0] == "wait_until_logged_in" for c in fake.calls)


def test_status_logged_in_when_fake_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    fake = FakeBrowserDriver(logged_in=True)
    code = main(["telegram", "status"], browser=fake)
    assert code == 0
    out = capsys.readouterr().out
    assert "logged-in" in out
    assert "qa-telegram" in out


def test_missing_agent_browser_exits_127(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("QA_BROWSER_BIN", "qa-cli-definitely-missing-agent-browser")
    code = main(["telegram", "status"])
    assert code == 127
    err = capsys.readouterr().err
    assert "npm i -g agent-browser && agent-browser install" in err


def test_failed_browser_does_not_report_logged_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # `false` exists on PATH but exits 1 — must not be treated as logged-out.
    monkeypatch.setenv("QA_BROWSER_BIN", "false")
    code = main(["telegram", "status"])
    assert code == 1
    captured = capsys.readouterr()
    assert "logged-out" not in captured.out
    assert "error:" in captured.err


def test_login_timeout_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))

    class TimeoutFake(FakeBrowserDriver):
        def wait_until_logged_in(self, timeout: float) -> None:
            raise TimeoutError("timed out waiting for Telegram Web QR login")

    code = main(["telegram", "login"], browser=TimeoutFake(logged_in=False))
    assert code == 1
    captured = capsys.readouterr()
    assert "timed out" in captured.err


def test_unknown_command_exits_2() -> None:
    assert main(["nope"]) == 2


def test_telegram_requires_subcommand() -> None:
    assert main(["telegram"]) == 2


@pytest.mark.skipif(
    os.environ.get("QA_TELEGRAM_LIVE") != "1",
    reason="live Telegram Web is opt-in; set QA_TELEGRAM_LIVE=1",
)
def test_live_status_skips_unless_env() -> None:
    # Opt-in only. Must not send chat messages. 127 if agent-browser is missing.
    code = main(["telegram", "status"])
    assert code in (0, 127)
