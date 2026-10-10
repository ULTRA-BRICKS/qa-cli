"""Telegram cloud-password (2FA) step: verdict detection, re-prompt, hint.

The fake page mirrors telegram-tt's AuthPassword/PasswordForm state machine:
after a submit the button shows its loading spinner while Telegram runs the
SRP check; a wrong password puts Telegram's own error text into the input
label (`.input-group.error`); a correct one removes `#auth-password-form`.
"""
import io
import itertools
import json
import subprocess

import pytest

from qa_cli import telegram
from qa_cli.browser import (
    AgentBrowserDriver,
    AuthPasswordRejectedError,
    AuthStatus,
    AuthStepRequiredError,
    PasswordStepState,
    _PASSWORD_STATE_JS,
)

INVALID = "Invalid password, please try again"


class PasswordPage:
    """One scripted frame list per submitted attempt; a frame is consumed
    per auth_status() poll and the last frame persists.

    Frames: 'checking', 'idle', 'done', 'error:<text>', 'error-checking:<text>'.
    """

    def __init__(self, scripts, hint=None):
        self.scripts = list(scripts)
        self.hint = hint
        self.submitted = []
        self.frames = ["idle"]
        self.frame = "idle"

    def open(self, url): pass
    def is_logged_in(self): return False
    def wait_for_qr(self, timeout): pass
    def diagnostic_dump(self): return "sanitized"

    def submit_password(self, password):
        self.submitted.append(password)
        self.frames = list(self.scripts[len(self.submitted) - 1])
        self.frame = "idle"

    def auth_status(self):
        if self.submitted:
            self.frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        if self.frame == "done":
            return AuthStatus(True, None)
        return AuthStatus(False, "auth-password-form")

    def password_state(self):
        kind, _, text = self.frame.partition(":")
        failed = kind.startswith("error")
        return PasswordStepState(
            present=self.frame != "done",
            checking=kind in ("checking", "error-checking"),
            error=text if failed else None,
            # One <label>: Telegram's error text replaces the hint.
            hint=None if failed else self.hint,
        )


@pytest.fixture
def terminal(monkeypatch, tmp_path):
    monkeypatch.setenv("QA_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "_interactive_input_available", lambda: True)
    monkeypatch.setattr(telegram.time, "sleep", lambda _s: None)
    out = io.StringIO()
    prompts = []

    def typed(*values):
        answers = iter(values)

        def getpass(prompt):
            prompts.append(out.getvalue())
            return next(answers)

        monkeypatch.setattr(telegram.getpass, "getpass", getpass)

    return out, prompts, typed


def test_slow_correct_password_is_not_reported_as_rejected(terminal):
    # The old bound gave up after five polls; Telegram may legitimately keep
    # its spinner up for longer (SRP check, DC round trip, main bundle load).
    out, prompts, typed = terminal
    typed("right")
    page = PasswordPage([["checking"] * 12 + ["done"]])
    telegram._run_qr_login_loop(page, 300, 0, out)
    assert page.submitted == ["right"]
    assert len(prompts) == 1
    assert "right" not in out.getvalue()


def test_telegram_error_label_is_reported_and_password_reprompted(terminal):
    out, prompts, typed = terminal
    typed("wrong", "right")
    page = PasswordPage([
        ["checking", f"error-checking:{INVALID}", f"error:{INVALID}"],
        ["checking", "done"],
    ])
    telegram._run_qr_login_loop(page, 300, 0, out)
    assert page.submitted == ["wrong", "right"]
    assert len(prompts) == 2
    assert INVALID not in prompts[0]
    assert INVALID in prompts[1]
    text = out.getvalue()
    assert "wrong" not in text.replace(INVALID, "") and "right" not in text


def test_waits_for_telegram_to_finish_loading_before_reprompting(terminal):
    # Telegram ignores submits while isLoading; the error appears while the
    # spinner is still up (GetPassword re-fetch), so re-prompting must wait.
    out, prompts, typed = terminal
    typed("wrong", "right")
    page = PasswordPage([
        [f"error-checking:{INVALID}"] * 6 + [f"error:{INVALID}"],
        ["done"],
    ])
    polls = []
    original = page.password_state
    page.password_state = lambda: polls.append(page.frame) or original()
    telegram._run_qr_login_loop(page, 300, 0, out)
    assert page.submitted == ["wrong", "right"]
    assert f"error:{INVALID}" in polls


def test_bounded_attempts_then_distinct_rejection_exit_code(terminal, capsys):
    from qa_cli.cli import main
    out, prompts, typed = terminal
    typed("bad-1", "bad-2", "bad-3", "bad-4")
    page = PasswordPage([[f"error:{INVALID}"]] * 4)
    assert main(["telegram", "login"], browser=page) == 1
    err = capsys.readouterr().err
    assert page.submitted == ["bad-1", "bad-2", "bad-3"]
    assert INVALID in err
    assert "bad-" not in err


def test_hint_is_shown_before_the_masked_prompt(terminal):
    out, prompts, typed = terminal
    typed("right")
    page = PasswordPage([["done"]], hint="first cat")
    telegram._run_qr_login_loop(page, 300, 0, out)
    assert "Hint: first cat" in prompts[0]


def test_hint_is_kept_on_reprompt_while_the_label_shows_the_error(terminal):
    out, prompts, typed = terminal
    typed("wrong", "right")
    page = PasswordPage([[f"error:{INVALID}"], ["done"]], hint="first cat")
    telegram._run_qr_login_loop(page, 300, 0, out)
    second_screen = prompts[1][len(prompts[0]):]
    assert INVALID in second_screen and "Hint: first cat" in second_screen


def test_no_hint_prints_nothing(terminal):
    out, prompts, typed = terminal
    typed("right")
    telegram._run_qr_login_loop(PasswordPage([["done"]]), 300, 0, out)
    assert "Hint" not in out.getvalue()


def test_still_checking_at_deadline_times_out_without_claiming_rejection(terminal, monkeypatch):
    out, prompts, typed = terminal
    typed("right")
    clock = itertools.count(0.0, 1.0)
    monkeypatch.setattr(telegram.time, "monotonic", lambda: next(clock))
    page = PasswordPage([["checking"]])
    with pytest.raises(TimeoutError) as caught:
        telegram._run_qr_login_loop(page, 60, 0, out)
    assert "still checking" in str(caught.value)
    assert page.submitted == ["right"]


def test_submit_that_never_starts_a_check_is_not_called_a_rejection(terminal):
    out, prompts, typed = terminal
    typed("right")
    page = PasswordPage([["idle"]])
    with pytest.raises(AuthStepRequiredError) as caught:
        telegram._run_qr_login_loop(page, 300, 0, out)
    assert not isinstance(caught.value, AuthPasswordRejectedError)
    assert "did not start checking" in str(caught.value)
    assert page.submitted == ["right"]


# --- _PASSWORD_STATE_JS against a DOM stub mirroring PasswordForm.tsx ------

_DOM_STUB = r"""
const page = PAGE;
const el = (props) => Object.assign({classList: {contains: (c) => (props.cls || '').split(' ').includes(c)}}, props);
const label = page.form && el({textContent: page.label});
const group = page.form && el({cls: page.group, querySelector: (s) => s === 'label' ? label : null});
const button = page.button !== undefined && el({cls: page.button});
const form = page.form && el({querySelector: (s) => s.includes('input-group') ? group : s.includes('button') ? button || null : null});
global.document = {
  documentElement: {lang: page.lang ?? 'en'},
  getElementById: (id) => id === 'auth-password-form' ? form || null : null,
};
console.log(SCRIPT);
"""


def _page_state(**page):
    script = _DOM_STUB.replace("PAGE", json.dumps(page)).replace("SCRIPT", _PASSWORD_STATE_JS)
    return json.loads(subprocess.check_output(["node", "-e", script], text=True))


def _driver_state(raw):
    driver = AgentBrowserDriver(binary="fake")
    driver._eval_json = lambda _js: raw
    return driver.password_state()


def test_state_js_reads_spinner_error_and_label():
    loading = _page_state(form=True, group="input-group password-input touched", label="Password", button="Button default primary loading")
    assert loading["present"] is True and loading["checking"] is True and loading["error"] is None
    failed = _page_state(form=True, group="input-group password-input touched error", label=INVALID, button="Button default primary")
    assert failed["checking"] is False and failed["error"] == INVALID
    hinted = _page_state(form=True, group="input-group password-input", label="first cat")
    assert hinted["label"] == "first cat" and hinted["checking"] is False
    assert _page_state(form=False) == {"present": False}


def test_driver_maps_default_placeholder_to_no_hint():
    base = {"present": True, "checking": False, "error": None, "lang": "en"}
    assert _driver_state({**base, "label": "Password"}).hint is None
    assert _driver_state({**base, "label": "first cat"}).hint == "first cat"
    # Non-English UI: the localized placeholder is indistinguishable from a hint.
    assert _driver_state({**base, "label": "Пароль", "lang": "ru"}).hint is None
    assert _driver_state({"present": False}) == PasswordStepState(False, False, None, None)


def test_driver_strips_terminal_control_characters_from_page_text():
    state = _driver_state({"present": True, "checking": False, "error": "bad\x1b[2Jtext", "label": None, "lang": "en"})
    assert state.error == "bad[2Jtext"


def test_password_longer_than_telegram_input_limit_is_not_truncated_silently():
    from qa_cli.auth_input import submission_commands
    submission_commands("password", "x" * 256)
    with pytest.raises(ValueError, match="256"):
        submission_commands("password", "x" * 257)
