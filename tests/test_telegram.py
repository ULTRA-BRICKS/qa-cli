"""Hermetic tests for `qa telegram login` / `qa telegram status`.

Accessed via: `pytest tests/test_telegram.py` (CI and local). Default pytest
injects FakeBrowserDriver — never Chromium, never web.telegram.org, never
Telegram.app. Live agent-browser is opt-in via QA_TELEGRAM_LIVE=1.

Assumptions: production `qa_cli.cli.main` is the unit under test. The fake
browser is only a boundary stub (records calls, writes a tiny PNG). Tests
never send chat messages.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from qa_cli.browser import AgentBrowserDriver, AuthStatus  # noqa: E402
from qa_cli.cli import main  # noqa: E402
from qa_cli.telegram import login, render_qr_ascii  # noqa: E402

TELEGRAM_WEB = "https://web.telegram.org/a/"

# 1x1 transparent PNG so screenshot() produces a real image file.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

FORBIDDEN_ARGV = ("-a", "Telegram", "Telegram.app", "playwright", "puppeteer", "npx")

_QR_PROBE_HARNESS = """
const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function makeEl(spec, parent) {
  const node = {
    width: spec.width || 0,
    height: spec.height || 0,
    clientWidth: spec.clientWidth != null ? spec.clientWidth : (spec.width || 0),
    clientHeight: spec.clientHeight != null ? spec.clientHeight : (spec.height || 0),
    naturalWidth: spec.naturalWidth != null ? spec.naturalWidth : (spec.width || 0),
    naturalHeight: spec.naturalHeight != null ? spec.naturalHeight : (spec.height || 0),
    className: spec.className || '',
    id: spec.id || '',
    alt: spec.alt || '',
    src: spec.src || '',
    parentElement: parent || null,
    getAttribute(name) {
      if (name === 'aria-label') return spec.ariaLabel || '';
      if (name === 'viewBox') return spec.viewBox || null;
      if (name === 'width') return spec.widthAttr != null ? String(spec.widthAttr) : (spec.width ? String(spec.width) : null);
      if (name === 'height') return spec.heightAttr != null ? String(spec.heightAttr) : (spec.height ? String(spec.height) : null);
      return null;
    },
    getContext() {
      return {
        getImageData() {
          const w = node.width;
          const h = node.height;
          const data = new Uint8ClampedArray(w * h * 4);
          const pattern = spec.pattern || 'blank';
          if (pattern === 'checker') {
            for (let y = 0; y < h; y++) {
              for (let x = 0; x < w; x++) {
                const on = ((x >> 3) + (y >> 3)) % 2 === 0;
                const i = (y * w + x) * 4;
                const v = on ? 0 : 255;
                data[i] = data[i + 1] = data[i + 2] = v;
                data[i + 3] = 255;
              }
            }
          }
          return { data };
        }
      };
    }
  };
  return node;
}
function withParent(spec) {
  const parent = spec.parent ? makeEl(spec.parent, null) : null;
  return makeEl(spec, parent);
}
const canvases = (payload.canvases || []).map(withParent);
const imgs = (payload.imgs || []).map(withParent);
const svgs = (payload.svgs || []).map(withParent);
global.document = {
  querySelector(sel) {
    if (sel === '.qr-container svg') {
      for (const s of svgs) {
        let p = s.parentElement;
        while (p) {
          const cls = typeof p.className === 'string' ? p.className : '';
          if (cls.includes('qr-container')) return s;
          p = p.parentElement;
        }
      }
      return null;
    }
    return null;
  },
  querySelectorAll(sel) {
    if (sel === 'canvas') return canvases;
    if (sel === 'img') return imgs;
    if (sel === 'svg') return svgs;
    return [];
  }
};
const result = eval(payload.js);
process.stdout.write(String(result));
"""

def _eval_qr_js(js: str, *, canvases=None, imgs=None, svgs=None) -> str:
    """Run production probe JS against a stub document (no Chromium)."""
    payload = json.dumps(
        {
            "js": js,
            "canvases": canvases or [],
            "imgs": imgs or [],
            "svgs": svgs or [],
        }
    )
    proc = subprocess.run(
        ["node", "-e", _QR_PROBE_HARNESS],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout or "node probe harness failed")
    return proc.stdout.strip()

def _is_qr_visible_against(
    driver: AgentBrowserDriver,
    monkeypatch: pytest.MonkeyPatch,
    canvases=None,
    imgs=None,
    svgs=None,
) -> bool:
    def fake_run(*args: str):
        assert args[0] == "eval"
        out = _eval_qr_js(args[1], canvases=canvases, imgs=imgs, svgs=svgs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(driver, "_run", fake_run)
    return driver.is_qr_visible()

_AUTH_STATUS_HARNESS = """
const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));
global.document = {
  querySelectorAll(sel) { return payload.formId ? [{ id: payload.formId }] : []; },
  querySelector(sel) {
    if (sel.includes('#LeftColumn')) return payload.hasShell ? {} : null;
    if (sel === '[id^="auth-"][id$="-form"]') {
      return payload.formId ? { id: payload.formId } : null;
    }
    if (sel === '.is-auth') {
      return payload.isAuth ? {} : null;
    }
    return null;
  }
};
const result = eval(payload.js);
process.stdout.write(String(result));
"""

def _eval_auth_status_js(js: str, *, form_id=None, is_auth: bool = False, has_shell: bool = False) -> dict:
    """Run production auth-status JS against a stub document (no Chromium)."""
    payload = json.dumps({"js": js, "formId": form_id, "isAuth": is_auth, "hasShell": has_shell})
    proc = subprocess.run(
        ["node", "-e", _AUTH_STATUS_HARNESS],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout or "auth status harness failed")
    return json.loads(proc.stdout.strip())


class FakeBrowserDriver:
    """Test double: records calls, never shells out."""

    def __init__(
        self,
        logged_in: bool = False,
        waiting_form=None,
        qr_tokens=None,
        become_logged_in_after: int = 1,
    ) -> None:
        self.logged_in = logged_in
        self.waiting_form = waiting_form
        self.calls: list[tuple] = []
        self.qr_ready = False
        self._qr_tokens = list(qr_tokens) if qr_tokens is not None else ["FAKE_QR_TOKEN"]
        self._become_logged_in_after = become_logged_in_after
        self._auth_status_calls = 0
        self.submitted_passwords: list[str] = []

    def open(self, url: str) -> None:
        self.calls.append(("open", url))

    def screenshot(self, path: str) -> None:
        if not self.qr_ready:
            raise AssertionError("screenshot called before QR was ready")
        self.calls.append(("screenshot", path))
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_TINY_PNG)

    def is_logged_in(self) -> bool:
        self.calls.append(("is_logged_in",))
        return self.logged_in

    def auth_status(self) -> AuthStatus:
        self.calls.append(("auth_status",))
        if not self.logged_in and self.qr_ready:
            self._auth_status_calls += 1
            if self._auth_status_calls > self._become_logged_in_after:
                self.logged_in = True
        if self.logged_in:
            return AuthStatus(logged_in=True, waiting_form=None)
        return AuthStatus(logged_in=False, waiting_form=self.waiting_form)

    def get_qr_token(self):
        self.calls.append(("get_qr_token",))
        if not self._qr_tokens:
            return None
        token = self._qr_tokens[0]
        if len(self._qr_tokens) > 1:
            self._qr_tokens.pop(0)
        return token

    def diagnostic_dump(self) -> str:
        self.calls.append(("diagnostic_dump",))
        return "title='Telegram' url='https://web.telegram.org/a/' text='Log in to Telegram by QR Code'"

    def submit_password(self, password: str) -> None:
        self.calls.append(("submit_password", password))
        self.submitted_passwords.append(password)

    def wait_for_qr(self, timeout: float) -> None:
        self.calls.append(("wait_for_qr", timeout))
        self.qr_ready = True

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
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
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
    assert any(c[0] == "auth_status" for c in fake.calls)
    assert "logged-in" in out

def test_login_screenshots_only_after_qr_ready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    fake = FakeBrowserDriver(logged_in=False)
    code = main(["telegram", "login"], browser=fake)
    assert code == 0
    names = [c[0] for c in fake.calls]
    assert "wait_for_qr" in names
    assert "screenshot" in names
    assert names.index("open") < names.index("wait_for_qr") < names.index("screenshot")
    qr = tmp_path / "telegram-qr.png"
    assert qr.is_file()
    assert str(qr) in capsys.readouterr().out

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
    assert not any(c[0] == "wait_for_qr" for c in fake.calls)
    assert not any(c[0] == "auth_status" for c in fake.calls)

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
    assert not any(c[0] == "wait_for_qr" for c in fake.calls)

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
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    times = iter([0.0, 1000.0])
    monkeypatch.setattr("qa_cli.telegram.time.monotonic", lambda: next(times, 100000.0))
    fake = FakeBrowserDriver(logged_in=False, become_logged_in_after=10**9)
    code = main(["telegram", "login"], browser=fake)
    assert code == 1
    captured = capsys.readouterr()
    assert "timed out" in captured.err

def test_login_timeout_includes_diagnostic_dump_and_last_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    times = iter([0.0, 1000.0])
    monkeypatch.setattr("qa_cli.telegram.time.monotonic", lambda: next(times, 100000.0))
    fake = FakeBrowserDriver(
        logged_in=False, waiting_form="auth-password-form", become_logged_in_after=10**9
    )
    code = main(["telegram", "login"], browser=fake)
    assert code == 1
    err = capsys.readouterr().err
    assert "auth-password-form" in err
    assert "title=" in err and "url=" in err
    assert any(c[0] == "diagnostic_dump" for c in fake.calls)

def test_login_qr_wait_timeout_skips_screenshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))

    class QrTimeoutFake(FakeBrowserDriver):
        def wait_for_qr(self, timeout: float) -> None:
            self.calls.append(("wait_for_qr", timeout))
            raise TimeoutError("timed out waiting for Telegram Web QR code")

    fake = QrTimeoutFake(logged_in=False)
    code = main(["telegram", "login"], browser=fake)
    assert code == 1
    captured = capsys.readouterr()
    assert "timed out" in captured.err
    assert "QR code" in captured.err
    assert not any(c[0] == "screenshot" for c in fake.calls)
    assert not (tmp_path / "telegram-qr.png").exists()

def test_is_qr_visible_rejects_blank_canvas(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = AgentBrowserDriver(binary="unused")
    blank = {
        "width": 80,
        "height": 80,
        "pattern": "blank",
        "parent": {"className": "qr-container"},
    }
    assert _is_qr_visible_against(driver, monkeypatch, canvases=[blank]) is False

def test_is_qr_visible_rejects_unrelated_canvas(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = AgentBrowserDriver(binary="unused")
    chart = {"width": 80, "height": 80, "pattern": "checker", "className": "chart"}
    assert _is_qr_visible_against(driver, monkeypatch, canvases=[chart]) is False

def test_is_qr_visible_accepts_painted_qr_canvas(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = AgentBrowserDriver(binary="unused")
    qr = {
        "width": 80,
        "height": 80,
        "pattern": "checker",
        "parent": {"className": "qr-container"},
    }
    assert _is_qr_visible_against(driver, monkeypatch, canvases=[qr]) is True

def test_is_qr_visible_accepts_qr_img(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = AgentBrowserDriver(binary="unused")
    img = {"width": 80, "height": 80, "alt": "QR code"}
    assert _is_qr_visible_against(driver, monkeypatch, imgs=[img]) is True

def test_is_qr_visible_accepts_qr_svg(monkeypatch: pytest.MonkeyPatch) -> None:
    # Live Telegram Web A draws the login QR as SVG inside .qr-container.
    driver = AgentBrowserDriver(binary="unused")
    svg = {"width": 280, "height": 280, "parent": {"className": "qr-container"}}
    assert _is_qr_visible_against(driver, monkeypatch, svgs=[svg]) is True

def test_is_qr_visible_rejects_unrelated_svg(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = AgentBrowserDriver(binary="unused")
    icon = {
        "width": 280,
        "height": 280,
        "className": "icon",
        "parent": {"className": "header"},
    }
    assert _is_qr_visible_against(driver, monkeypatch, svgs=[icon]) is False

def test_is_qr_visible_accepts_svg_beside_tiny_lottie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Live page: 54x54 tlottie qr-plane plus 280x280 SVG QR. Tiny canvas must
    # not hide the SVG.
    driver = AgentBrowserDriver(binary="unused")
    lottie = {
        "width": 54,
        "height": 54,
        "pattern": "checker",
        "className": "tlottie-canvas",
        "parent": {"className": "AnimatedSticker qr-plane"},
    }
    svg = {"width": 280, "height": 280, "parent": {"className": "qr-container"}}
    assert _is_qr_visible_against(
        driver, monkeypatch, canvases=[lottie], svgs=[svg]
    ) is True



def test_is_qr_visible_accepts_viewbox_only_svg(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = AgentBrowserDriver(binary="unused")
    svg = {
        "viewBox": "0 0 280 280",
        "clientWidth": 280,
        "clientHeight": 280,
        "parent": {"className": "qr-container"},
    }
    assert _is_qr_visible_against(driver, monkeypatch, svgs=[svg]) is True


def test_wait_for_qr_detects_svg_after_delayed_render(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = AgentBrowserDriver(binary="unused")
    polls = {"n": 0}

    def fake_qr() -> bool:
        polls["n"] += 1
        return polls["n"] >= 3

    monkeypatch.setattr(driver, "auth_status", lambda: AuthStatus(logged_in=False, waiting_form=None))
    monkeypatch.setattr(driver, "is_qr_visible", fake_qr)
    monkeypatch.setattr("qa_cli.browser.time.sleep", lambda _s: None)
    driver.wait_for_qr(60.0)
    assert polls["n"] >= 3


def test_issue11_old_canvas_only_probe_misses_telegram_svg() -> None:
    # qa-cli 0.1.0 only probed canvas/img; Telegram Web A paints the login QR as SVG.
    old_js = (
        "(() => {"
        "  const canvases = Array.from(document.querySelectorAll('canvas'));"
        "  for (const c of canvases) {"
        "    const w = Math.max(c.width || 0, c.clientWidth || 0);"
        "    const h = Math.max(c.height || 0, c.clientHeight || 0);"
        "    if (w >= 80 && h >= 80) return 'qr-visible';"
        "  }"
        "  const imgs = Array.from(document.querySelectorAll('img'));"
        "  for (const img of imgs) {"
        "    const hay = ((img.alt || '') + ' ' + (img.className || '') + ' '"
        "      + (img.id || '') + ' ' + (img.src || '')).toLowerCase();"
        "    const w = Math.max(img.naturalWidth || 0, img.clientWidth || 0);"
        "    const h = Math.max(img.naturalHeight || 0, img.clientHeight || 0);"
        "    if (/qr/.test(hay) && w >= 80 && h >= 80) return 'qr-visible';"
        "  }"
        "  return 'qr-missing';"
        "})()"
    )
    from qa_cli.browser import _QR_VISIBLE_JS

    lottie = {
        "width": 54,
        "height": 54,
        "pattern": "checker",
        "className": "tlottie-canvas",
        "parent": {"className": "AnimatedSticker qr-plane"},
    }
    svg = {"width": 280, "height": 280, "parent": {"className": "qr-container"}}
    assert _eval_qr_js(old_js, canvases=[lottie], svgs=[svg]) == "qr-missing"
    assert _eval_qr_js(_QR_VISIBLE_JS, canvases=[lottie], svgs=[svg]) == "qr-visible"

def test_wait_for_qr_returns_when_logged_in_without_qr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = AgentBrowserDriver(binary="unused")
    polls = {"n": 0}

    def fake_auth_status() -> AuthStatus:
        polls["n"] += 1
        return AuthStatus(logged_in=polls["n"] >= 2, waiting_form=None)

    monkeypatch.setattr(driver, "auth_status", fake_auth_status)
    monkeypatch.setattr(driver, "is_qr_visible", lambda: False)
    monkeypatch.setattr("qa_cli.browser.time.sleep", lambda _s: None)
    driver.wait_for_qr(5.0)


def test_wait_for_qr_returns_immediately_on_non_qr_auth_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A persisted session resuming straight into auth-password-form (no QR
    # ever rendered) must not dead-end waiting for QR pixels that will
    # never appear (regression: previously hung the full timeout here).
    driver = AgentBrowserDriver(binary="unused")

    def fail_if_called() -> bool:
        raise AssertionError("is_qr_visible should not be polled for a non-QR auth form")

    monkeypatch.setattr(
        driver, "auth_status", lambda: AuthStatus(logged_in=False, waiting_form="auth-password-form")
    )
    monkeypatch.setattr(driver, "is_qr_visible", fail_if_called)
    monkeypatch.setattr("qa_cli.browser.time.sleep", lambda _s: None)
    driver.wait_for_qr(5.0)

def test_login_skips_screenshot_when_logged_in_during_qr_wait(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))

    class RestoredDuringWait(FakeBrowserDriver):
        def wait_for_qr(self, timeout: float) -> None:
            self.calls.append(("wait_for_qr", timeout))
            self.logged_in = True

        def screenshot(self, path: str) -> None:
            raise AssertionError("screenshot must not run when session restored without QR")

    fake = RestoredDuringWait(logged_in=False)
    code = main(["telegram", "login"], browser=fake)
    assert code == 0
    assert not (tmp_path / "telegram-qr.png").exists()
    out = capsys.readouterr().out
    assert "logged-in" in out
    assert not any(c[0] == "screenshot" for c in fake.calls)
    assert not any(c[0] == "auth_status" for c in fake.calls)

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

# --- auth_status() / _AUTH_STATUS_JS -------------------------------------

def test_auth_status_js_detects_qr_form() -> None:
    from qa_cli.browser import _AUTH_STATUS_JS

    result = _eval_auth_status_js(_AUTH_STATUS_JS, form_id="auth-qr-form")
    assert result == {"loggedIn": False, "waitingForm": "auth-qr-form"}

def test_auth_status_js_reports_unsupported_auth_step() -> None:
    # e.g. Telegram requiring a 2FA cloud password after a successful QR scan —
    # must be reported distinctly, not conflated with "still waiting for scan".
    from qa_cli.browser import _AUTH_STATUS_JS

    result = _eval_auth_status_js(_AUTH_STATUS_JS, form_id="auth-password-form")
    assert result == {"loggedIn": False, "waitingForm": "auth-password-form"}

def test_auth_status_js_detects_still_authenticating_without_form() -> None:
    from qa_cli.browser import _AUTH_STATUS_JS

    result = _eval_auth_status_js(_AUTH_STATUS_JS, is_auth=True)
    assert result == {"loggedIn": False, "waitingForm": None}

def test_auth_status_js_detects_logged_in_when_auth_ui_gone() -> None:
    # Telegram Web A removes .is-auth entirely once authorizationStateReady;
    # this app has no #LeftColumn/#column-left, so absence is the signal.
    from qa_cli.browser import _AUTH_STATUS_JS

    result = _eval_auth_status_js(_AUTH_STATUS_JS, has_shell=True)
    assert result == {"loggedIn": True, "waitingForm": None}

def test_auth_status_decodes_agent_browser_json(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = AgentBrowserDriver(binary="unused")

    def fake_run(*args: str):
        assert args[0] == "eval"
        inner = json.dumps({"loggedIn": False, "waitingForm": "auth-password-form"})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(inner), stderr="")

    monkeypatch.setattr(driver, "_run", fake_run)
    assert driver.auth_status() == AuthStatus(logged_in=False, waiting_form="auth-password-form")

def test_get_qr_token_decodes_agent_browser_json(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = AgentBrowserDriver(binary="unused")

    def fake_run(*args: str):
        inner = json.dumps({"token": "AQABC123"})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(inner), stderr="")

    monkeypatch.setattr(driver, "_run", fake_run)
    assert driver.get_qr_token() == "AQABC123"

def test_get_qr_token_returns_none_before_hook_captures_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = AgentBrowserDriver(binary="unused")

    def fake_run(*args: str):
        inner = json.dumps({"token": None})
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(inner), stderr="")

    monkeypatch.setattr(driver, "_run", fake_run)
    assert driver.get_qr_token() is None

def test_diagnostic_dump_formats_page_state(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = AgentBrowserDriver(binary="unused")

    def fake_run(*args: str):
        inner = json.dumps(
            {"title": "Telegram", "url": TELEGRAM_WEB, "text": "Log in to Telegram by QR Code"}
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(inner), stderr="")

    monkeypatch.setattr(driver, "_run", fake_run)
    dump = driver.diagnostic_dump()
    assert "Telegram" in dump
    assert TELEGRAM_WEB in dump
    assert "Log in to Telegram by QR Code" not in dump

# --- terminal QR rendering -------------------------------------------------

def test_render_qr_ascii_produces_multiline_unicode_block() -> None:
    art = render_qr_ascii("tg://login?token=ABCDEFGHIJ")
    lines = art.splitlines()
    assert len(lines) > 5
    assert any(ch in art for ch in "█▄▀")

def test_login_redraws_terminal_qr_on_token_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    fake = FakeBrowserDriver(
        logged_in=False, qr_tokens=["TOKEN_A", "TOKEN_B"], become_logged_in_after=3
    )
    buf = io.StringIO()
    code = login(fake, out=buf)
    assert code == 0
    rendered = buf.getvalue()
    # Exactly one in-place redraw: the token rotates from TOKEN_A to TOKEN_B once.
    assert "\x1b[2J" not in rendered  # redirected output is not a TUI
    assert [c[0] for c in fake.calls].count("screenshot") == 2

def test_login_reports_unsupported_auth_step_distinctly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Code, phone and password forms are now handled in the terminal;
    # every other auth step this CLI can't automate must still escalate
    # exactly as before.
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    fake = FakeBrowserDriver(
        logged_in=False, waiting_form="auth-registration-form", become_logged_in_after=99  # gitleaks:allow -- synthetic DOM form id, not a credential
    )
    buf = io.StringIO()
    from qa_cli.browser import AuthStepRequiredError
    with pytest.raises(AuthStepRequiredError, match="auth-registration-form"):
        login(fake, out=buf)
    rendered = buf.getvalue()
    assert not any(c[0] == "screenshot" for c in fake.calls)
    assert not any(c[0] == "submit_password" for c in fake.calls)


# ---- login timeout + QR hook (qa-cli#31) ------------------------------------


@pytest.fixture(autouse=True)
def _no_host_login_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # The operator's own QA_TELEGRAM_LOGIN_TIMEOUT must not leak into tests.
    monkeypatch.delenv("QA_TELEGRAM_LOGIN_TIMEOUT", raising=False)


class DeadlineRecordingFake(FakeBrowserDriver):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.deadlines: list = []

    def set_deadline(self, deadline) -> None:
        self.deadlines.append(deadline)


def _clock(monkeypatch: pytest.MonkeyPatch, *ticks: float) -> None:
    # login's QR loop reads monotonic() once for the deadline, then once per poll.
    it = iter(ticks)
    monkeypatch.setattr("qa_cli.telegram.time.monotonic", lambda: next(it, 10.0**12))


def _screenshots(fake: FakeBrowserDriver) -> int:
    return [c[0] for c in fake.calls].count("screenshot")


def test_login_timeout_flag_extends_the_qr_wait(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    # 599 s in: past the 300 s default, inside --timeout 600; 601 s: past both.
    _clock(monkeypatch, 0.0, 599.0, 601.0)
    fake = FakeBrowserDriver(logged_in=False, become_logged_in_after=10**9)
    code = main(["telegram", "login", "--timeout", "600"], browser=fake)
    assert code == 1
    assert "timed out" in capsys.readouterr().err
    assert _screenshots(fake) == 1


def test_login_timeout_zero_waits_forever_without_a_browser_deadline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    _clock(monkeypatch, 0.0, 10.0**6, 10.0**7, 10.0**8)
    fake = DeadlineRecordingFake(logged_in=False, become_logged_in_after=3)
    code = main(["telegram", "login", "--timeout", "0"], browser=fake)
    assert code == 0
    assert "logged-in" in capsys.readouterr().out
    assert all(d is None for d in fake.deadlines)


def test_login_timeout_env_applies_and_the_flag_overrides_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    monkeypatch.setenv("QA_TELEGRAM_LOGIN_TIMEOUT", "600")
    _clock(monkeypatch, 0.0, 599.0, 601.0)
    fake = FakeBrowserDriver(logged_in=False, become_logged_in_after=10**9)
    assert main(["telegram", "login"], browser=fake) == 1
    assert _screenshots(fake) == 1

    _clock(monkeypatch, 0.0, 599.0, 601.0)
    fake = FakeBrowserDriver(logged_in=False, become_logged_in_after=10**9)
    assert main(["telegram", "login", "--timeout", "300"], browser=fake) == 1
    assert _screenshots(fake) == 0
    capsys.readouterr()


@pytest.mark.parametrize("value", ["-1", "abc", "nan", "inf"])
def test_login_timeout_flag_rejects_bad_values(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeBrowserDriver(logged_in=True)
    assert main(["telegram", "login", "--timeout", value], browser=fake) == 2
    assert "--timeout" in capsys.readouterr().err
    assert fake.calls == []


def test_login_timeout_env_rejects_bad_values(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_TELEGRAM_LOGIN_TIMEOUT", "5m")
    fake = FakeBrowserDriver(logged_in=True)
    assert main(["telegram", "login"], browser=fake) == 2
    assert "QA_TELEGRAM_LOGIN_TIMEOUT" in capsys.readouterr().err
    assert fake.calls == []


def _wait_for_files(directory: Path, count: int) -> list[Path]:
    # The hook runs detached (login never waits on it): give it a moment to land.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        found = sorted(directory.glob("qr-*.png"))
        if len(found) >= count:
            return found
        time.sleep(0.05)
    return sorted(directory.glob("qr-*.png"))


def test_login_qr_hook_runs_with_the_png_on_every_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    seen = tmp_path / "seen"
    seen.mkdir()
    # $0 = the seen dir, $1 = the PNG path login appends; cp to a dotfile then mv,
    # so a qr-*.png only appears once fully copied.
    hook = f"sh -c 'cp \"$1\" \"$0/.part-$$\" && mv \"$0/.part-$$\" \"$0/qr-$$.png\"' {seen}"
    fake = FakeBrowserDriver(logged_in=False, qr_tokens=["TOKEN_A", "TOKEN_B"], become_logged_in_after=3)
    code = main(["telegram", "login", "--qr-hook", hook], browser=fake)
    assert code == 0
    copies = _wait_for_files(seen, 2)
    assert len(copies) == 2
    assert all(p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for p in copies)


def test_login_qr_hook_failure_never_aborts_login_and_is_reported_with_its_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    fake = FakeBrowserDriver(logged_in=False, become_logged_in_after=3)
    hook = "sh -c 'echo boom >&2; echo chatter; exit 3' _"
    assert main(["telegram", "login", "--qr-hook", hook], browser=fake) == 0
    captured = capsys.readouterr()
    assert "logged-in" in captured.out
    assert "chatter" not in captured.out
    assert "warning: --qr-hook exited 3: boom" in captured.err


def test_login_qr_hook_killed_by_a_signal_is_reported_as_such(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    fake = FakeBrowserDriver(logged_in=False, become_logged_in_after=3)
    assert main(["telegram", "login", "--qr-hook", "sh -c 'kill -9 $$' _"], browser=fake) == 0
    assert "warning: --qr-hook was killed by signal 9" in capsys.readouterr().err


def test_login_qr_hook_runs_that_hang_are_capped_instead_of_piling_up(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # With --timeout 0 a stalled uploader must not stack one process (and one
    # open stderr file) per QR rotation until the login runs out of descriptors.
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    tokens = [f"TOKEN_{i}" for i in range(8)]
    fake = FakeBrowserDriver(logged_in=False, qr_tokens=tokens, become_logged_in_after=8)
    assert main(["telegram", "login", "--qr-hook", "sh -c 'sleep 1' _"], browser=fake) == 0
    assert [c[0] for c in fake.calls].count("screenshot") == 8
    err = capsys.readouterr().err
    assert err.count("skipping this QR") == 4


def test_login_qr_hook_run_older_than_its_max_age_is_killed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hung upload of an expired QR is moot: it is killed, so it neither blocks
    # forwarding newer QRs nor grows its stderr file for the rest of a long wait.
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    monkeypatch.setattr("qa_cli.telegram._QrHook.MAX_AGE", 0.0)
    monkeypatch.setattr("qa_cli.telegram._QrHook.FINAL_GRACE", 0.1)
    tokens = [f"TOKEN_{i}" for i in range(8)]
    fake = FakeBrowserDriver(logged_in=False, qr_tokens=tokens, become_logged_in_after=8)
    assert main(["telegram", "login", "--qr-hook", "sh -c 'sleep 30' _"], browser=fake) == 0
    err = capsys.readouterr().err
    assert "skipping this QR" not in err
    assert "--qr-hook ran over 0 s; killed" in err


def test_login_qr_hook_still_running_when_login_ends_is_killed_after_the_grace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _s: None)
    monkeypatch.setattr("qa_cli.telegram._QrHook.FINAL_GRACE", 0.2)
    pids = tmp_path / "pids"
    pids.mkdir()
    # The hook's child (sleep) shares its session, so the whole group must go.
    hook = f"sh -c 'sleep 30 & echo $! > \"$0/child\"; wait' {pids}"
    fake = FakeBrowserDriver(logged_in=False, become_logged_in_after=3)
    assert main(["telegram", "login", "--qr-hook", hook], browser=fake) == 0
    assert "--qr-hook was still running when the login ended; killed" in capsys.readouterr().err
    child = int((pids / "child").read_text())
    # Killed with its group; init reaps the orphaned sleep shortly after.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
    else:
        pytest.fail(f"the hook's child {child} outlived the login")


def test_login_qr_hook_that_is_not_installed_exits_2_before_opening_the_browser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeBrowserDriver(logged_in=False)
    assert main(["telegram", "login", "--qr-hook", "no-such-qr-hook-binary --flag"], browser=fake) == 2
    assert "no-such-qr-hook-binary" in capsys.readouterr().err
    assert fake.calls == []


def test_login_help_documents_timeout_and_qr_hook(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["telegram", "login", "--help"]) == 0
    out = capsys.readouterr().out
    assert "--timeout" in out
    assert "QA_TELEGRAM_LOGIN_TIMEOUT" in out
    assert "--qr-hook" in out
