import io
import json
import subprocess
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa_cli.browser import AgentBrowserDriver, BrowserError, _AUTH_STATUS_JS
from qa_cli.telegram import _TerminalQrPainter

def test_blank_page_is_not_authenticated():
    script = "global.location=new URL('https://web.telegram.org/a/');global.document={readyState:'complete',querySelector:()=>null,querySelectorAll:()=>[]};console.log(" + _AUTH_STATUS_JS + ")"
    value = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert value["loggedIn"] is False

def test_subprocess_has_finite_timeout(monkeypatch):
    seen = {}
    def run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    AgentBrowserDriver(binary="fake").open("https://web.telegram.org/a/")
    assert 0 < seen.get("timeout", 0) <= 45

def test_timeout_does_not_leak_command_or_page_content(monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10, output="PRIVATE_BODY", stderr="PRIVATE_ERROR")
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(BrowserError) as error:
        AgentBrowserDriver(binary="fake")._run("eval", "PRIVATE_TOKEN")
    assert "PRIVATE" not in str(error.value)

def test_first_qr_frame_does_not_wait_for_library_rotation(monkeypatch):
    driver = AgentBrowserDriver(binary="fake")
    def probe(script):
        return {"token": "SYNTHETIC"} if "BarcodeDetector" in script else {"token": None}
    monkeypatch.setattr(driver, "_eval_json", probe)
    assert driver.get_qr_token() == "SYNTHETIC"

def test_string_false_is_not_authenticated(monkeypatch):
    driver = AgentBrowserDriver(binary="fake")
    monkeypatch.setattr(driver, "_eval_json", lambda script: {"loggedIn": "false", "waitingForm": None})
    assert driver.is_logged_in() is False


def test_tui_restores_screen_and_cursor():
    class Terminal(io.StringIO):
        def isatty(self): return True
    out = Terminal()
    painter = _TerminalQrPainter(out)
    try:
        painter.render("Waiting", "SYNTHETIC_QR")
        painter.render("Refreshed", "SYNTHETIC_QR_2")
    finally:
        painter.close()
    text = out.getvalue()
    assert text.startswith("\x1b[?1049h\x1b[?25l")
    assert "\x1b[2J\x1b[H" in text
    assert text.endswith("\x1b[?25h\x1b[?1049l")


def test_redirected_output_does_not_control_terminal():
    out = io.StringIO()
    painter = _TerminalQrPainter(out)
    painter.render("Waiting")
    painter.render("Refreshed")
    painter.close()
    assert "\x1b" not in out.getvalue()


def test_failed_qr_hook_is_actionable_not_silent(monkeypatch):
    driver = AgentBrowserDriver(binary="fake")
    monkeypatch.setattr(driver, "_eval_json", lambda script: {"token": None, "error": "PRIVATE_TOKEN"})
    times = iter([0.0, 16.0])
    monkeypatch.setattr("qa_cli.browser.time.monotonic", lambda: next(times))
    assert driver.get_qr_token() is None
    with pytest.raises(BrowserError, match="capture is unavailable") as error:
        driver.get_qr_token()
    assert "PRIVATE" not in str(error.value)


def test_transient_qr_hook_failure_recovers(monkeypatch):
    driver = AgentBrowserDriver(binary="fake")
    values = iter([{"token": None}, {"error": "loading"}, {"token": None}, {"token": "SYNTHETIC"}])
    monkeypatch.setattr(driver, "_eval_json", lambda script: next(values))
    assert driver.get_qr_token() is None
    assert driver.get_qr_token() == "SYNTHETIC"
    assert driver._qr_failure_since is None


def test_hidden_shell_does_not_authenticate():
    script = """global.location=new URL('https://web.telegram.org/a/');global.getComputedStyle=()=>({visibility:'hidden',opacity:'1',display:'block'});
    const shell={getClientRects:()=>[1]};
    global.document={querySelector:(s)=>s.includes('#Main')?shell:null,querySelectorAll:()=>[]};
    console.log(""" + _AUTH_STATUS_JS + ")"
    assert json.loads(subprocess.check_output(["node","-e",script],text=True))["loggedIn"] is False


def test_unsupported_decoder_is_cached(monkeypatch):
    driver=AgentBrowserDriver(binary="fake")
    seen=[]
    def evaluate(script):
        seen.append(script)
        return {"supported":False,"token":None} if "BarcodeDetector" in script else {"token":None}
    monkeypatch.setattr(driver,"_eval_json",evaluate)
    driver.get_qr_token(); driver.get_qr_token()
    assert sum("BarcodeDetector" in x for x in seen)==1


def test_browser_nonzero_error_never_discloses_payload(monkeypatch):
    monkeypatch.setattr(subprocess,"run",lambda argv,**kw:subprocess.CompletedProcess(argv,1,"PRIVATE_RESPONSE","PRIVATE_ERROR"))
    with pytest.raises(BrowserError) as error: AgentBrowserDriver(binary="fake")._run("eval","PRIVATE_JS")
    assert "PRIVATE" not in str(error.value)


def test_invalid_hook_token_is_not_rendered(monkeypatch):
    driver=AgentBrowserDriver(binary="fake")
    monkeypatch.setattr(driver,"_eval_json",lambda script:{"token":"secret\n<script>"})
    assert driver.get_qr_token() is None


def test_transient_auth_form_can_reach_logged_in(tmp_path,monkeypatch):
    from qa_cli.browser import AuthStatus
    from qa_cli.telegram import login
    monkeypatch.setenv("QA_CONFIG_DIR",str(tmp_path))
    class Browser:
        def open(self,url): pass
        def is_logged_in(self): return False
        def wait_for_qr(self,timeout): pass
        def auth_status(self): return next(self.states)
        states=iter([AuthStatus(False,"auth-phone-number-form"),AuthStatus(True,None)])
    out=io.StringIO()
    assert login(Browser(),poll_interval=0,out=out)==0
    assert "logged-in" in out.getvalue()


def test_persistent_manual_step_returns_code_three(tmp_path,monkeypatch):
    # auth-password-form is now auto-handled (terminal password entry, see
    # test_password_step_* below); any OTHER persistent manual step must
    # still escalate to AuthStepRequiredError / exit 3 exactly as before.
    from qa_cli.browser import AuthStatus
    from qa_cli.cli import main
    monkeypatch.setenv("QA_CONFIG_DIR",str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep",lambda _:None)
    class Browser:
        def open(self,url): pass
        def is_logged_in(self): return False
        def wait_for_qr(self,timeout): pass
        def auth_status(self): return AuthStatus(False,"auth-phone-number-form")
    assert main(["telegram","login"],browser=Browser())==3


def test_password_step_prompts_masked_and_submits_to_page(tmp_path, monkeypatch):
    from qa_cli.browser import AuthStatus
    from qa_cli.telegram import login
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _: None)
    monkeypatch.setattr("qa_cli.telegram.getpass.getpass", lambda *a, **k: "s3cret-test-only")
    monkeypatch.setattr("qa_cli.telegram._interactive_input_available", lambda: True)
    submitted = []
    class Browser:
        def open(self, url): pass
        def is_logged_in(self): return False
        def wait_for_qr(self, timeout): pass
        def auth_status(self): return next(self.states)
        def submit_password(self, password): submitted.append(password)
        states = iter([
            AuthStatus(False, "auth-password-form"),
            AuthStatus(False, "auth-password-form"),  # 2nd poll -> debounce triggers the prompt
            AuthStatus(True, None),  # cleared after submission
        ])
    out = io.StringIO()
    assert login(Browser(), poll_interval=0, out=out) == 0
    # The fake driver's submit_password is the ONLY place the password
    # travels through; no other recorded interaction (or terminal output)
    # ever sees it.
    assert submitted == ["s3cret-test-only"]
    assert "s3cret-test-only" not in out.getvalue()


def test_submit_password_sends_via_stdin_never_argv(monkeypatch):
    driver = AgentBrowserDriver(binary="fake")
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps([{"success":True}] * 4), "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    driver.submit_password("PRIVATE_TEST_PASSWORD_XYZ")
    assert len(calls) == 1
    argv, kwargs = calls[0]
    # Structural proof: the password is not a substring of any argv element
    # (visible to other local processes via ps/proc); it only travels via
    # the subprocess `input=` kwarg (stdin).
    assert not any("PRIVATE_TEST_PASSWORD_XYZ" in part for part in argv)
    assert "batch" in argv and "--bail" in argv
    assert kwargs.get("input") is not None
    assert "PRIVATE_TEST_PASSWORD_XYZ" in kwargs["input"]


# --- _SUBMIT_PASSWORD_JS (stubs Node's DOM; no Chromium) -------------------

def test_password_step_survives_a_slow_correct_password_without_false_rejection(tmp_path, monkeypatch):
    # Telegram's own password check is a network round trip: the form can
    # legitimately stay mounted for a couple of polls after a CORRECT
    # password before it clears. A single immediate poll must not
    # misreport this as a rejection.
    from qa_cli.browser import AuthStatus
    from qa_cli.telegram import login
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _: None)
    monkeypatch.setattr("qa_cli.telegram.getpass.getpass", lambda *a, **k: "correct-but-slow")
    monkeypatch.setattr("qa_cli.telegram._interactive_input_available", lambda: True)
    class Browser:
        def open(self, url): pass
        def is_logged_in(self): return False
        def wait_for_qr(self, timeout): pass
        def submit_password(self, password): pass
        def auth_status(self): return next(self.states)
        states = iter([
            AuthStatus(False, "auth-password-form"),
            AuthStatus(False, "auth-password-form"),  # debounce poll #2 -> triggers the prompt
            AuthStatus(False, "auth-password-form"),  # confirm poll #1: still validating server-side
            AuthStatus(False, "auth-password-form"),  # confirm poll #2: still validating
            AuthStatus(True, None),                    # confirm poll #3: cleared -- correct password
        ])
    out = io.StringIO()
    assert login(Browser(), poll_interval=0, out=out) == 0
    assert "logged-in" in out.getvalue()


def test_password_step_rejected_reports_clear_error_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    from qa_cli.browser import AuthStatus
    from qa_cli.cli import main
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _: None)
    monkeypatch.setattr("qa_cli.telegram.getpass.getpass", lambda *a, **k: "wrong-password")
    monkeypatch.setattr("qa_cli.telegram._interactive_input_available", lambda: True)
    class Browser:
        def open(self, url): pass
        def is_logged_in(self): return False
        def wait_for_qr(self, timeout): pass
        def submit_password(self, password): pass
        def auth_status(self): return AuthStatus(False, "auth-password-form")
    code = main(["telegram", "login"], browser=Browser())
    assert code == 1
    err = capsys.readouterr().err
    assert "rejected the password" in err
    assert "wrong-password" not in err


def test_password_step_no_tty_fails_clearly_instead_of_hanging(tmp_path, monkeypatch, capsys):
    from qa_cli.browser import AuthStatus
    from qa_cli.cli import main
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("qa_cli.telegram.time.sleep", lambda _: None)
    def raise_eof(*_a, **_k):
        raise EOFError()
    monkeypatch.setattr("qa_cli.telegram.getpass.getpass", raise_eof)
    monkeypatch.setattr("qa_cli.telegram._interactive_input_available", lambda: True)
    class Browser:
        def open(self, url): pass
        def is_logged_in(self): return False
        def wait_for_qr(self, timeout): pass
        def auth_status(self): return AuthStatus(False, "auth-password-form")
    code = main(["telegram", "login"], browser=Browser())
    assert code == 3
    err = capsys.readouterr().err
    assert "interactive" in err


def test_qr_file_is_private(tmp_path,monkeypatch):
    from qa_cli.browser import AuthStatus
    from qa_cli.telegram import login
    monkeypatch.setenv("QA_CONFIG_DIR",str(tmp_path))
    class Browser:
        def open(self,url): pass
        def is_logged_in(self): return False
        def wait_for_qr(self,timeout): pass
        def auth_status(self): return next(self.states)
        def get_qr_token(self): return "SYNTHETIC"
        def screenshot(self,path): Path(path).write_bytes(b"SYNTHETIC_NOT_IMAGE")
        states=iter([AuthStatus(False,"auth-qr-form"),AuthStatus(True,None)])
    assert login(Browser(),poll_interval=0,out=io.StringIO())==0
    assert (tmp_path/"telegram-qr.png").stat().st_mode & 0o777 == 0o600


def test_qr_cache_uses_the_frame_that_was_actually_decoded():
    from qa_cli.browser import _QR_IMAGE_TOKEN_JS
    harness = r"""
const svg={markup:'old-frame'};
global.window={}; global.Blob=class {};
global.URL={createObjectURL:()=>"blob:test",revokeObjectURL:()=>{}};
global.XMLSerializer=class{serializeToString(node){return node.markup;}};
global.Image=class{constructor(){this.width=280;this.height=280;} async decode(){svg.markup='new-frame';}};
global.BarcodeDetector=class{static async getSupportedFormats(){return ['qr_code'];} async detect(){return [{rawValue:'tg://login?token=SYNTHETIC_OLD'}];}};
global.document={querySelector:(selector)=>selector==='.qr-container svg'?svg:null,createElement:()=>({getContext:()=>({drawImage:()=>{}})})};
"""
    script=harness+"Promise.resolve("+_QR_IMAGE_TOKEN_JS+").then(()=>console.log(JSON.stringify(window.__qaQrImageCache)))"
    result=json.loads(subprocess.check_output(["node","-e",script],text=True))
    assert result=={"markup":"old-frame","token":"SYNTHETIC_OLD"}
