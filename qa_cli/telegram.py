"""Telegram Web QR login and session status.

Accessed via: `qa telegram login` and `qa telegram status` (qa_cli.cli.main).

Assumptions: a BrowserDriver is provided (AgentBrowserDriver in production,
a fake in hermetic tests). Config dir is ~/.config/qa-cli unless QA_CONFIG_DIR
is set. Never launches Telegram.app and never sends chat messages.

login() renders the live QR as a terminal Unicode block (via the `qrcode`
package) instead of only writing a screenshot: Telegram Web A rotates the QR
token roughly every 20-30s, so a screenshot-only flow can go stale before the
user notices it. The render loop polls browser.get_qr_token() and redraws in
place (ANSI clear + reprint) whenever the token changes, and polls
browser.auth_status() to detect both success and any Telegram auth substep
this CLI cannot automate, reporting it distinctly instead of hanging
silently until timeout -- except Telegram's cloud-password (2FA) step,
`auth-password-form`, which IS handled: once it persists across the usual
two-poll debounce, `_prompt_and_submit_password` prompts for it with
`getpass.getpass` (masked, never echoed/logged/written anywhere) and submits
it directly into the live page via `browser.submit_password`. The PNG
screenshot is still written on every QR redraw for tooling that reads the
file directly.
"""
from __future__ import annotations

import contextlib
import getpass
import math
import warnings
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import IO, Optional, Sequence

import qrcode

from qa_cli.browser import (
    SESSION_NAME,
    TELEGRAM_WEB_URL,
    AUTH_PASSWORD_FORM_ID,
    AUTH_QR_FORM_ID,
    AuthPasswordRejectedError,
    AuthStatus,
    AuthStepRequiredError,
    BrowserDriver,
)

from qa_cli.auth_input import CODE_FORM, PHONE_FORM

DEFAULT_LOGIN_TIMEOUT = 300.0
LOGIN_TIMEOUT_ENV = "QA_TELEGRAM_LOGIN_TIMEOUT"
DEFAULT_QR_WAIT_TIMEOUT = 60.0
DEFAULT_POLL_INTERVAL = 1.0

_QR_URL_PREFIX = "tg://login?token="

_MANUAL_STEP_FALLBACK = "finish this step manually in a headed browser session, then re-run"

# Telegram's cloud-password check is a network round trip (its own SRP
# verification API call), not instant -- a single poll right after submit
# would misreport a slow-but-correct password as rejected. Bounded so a
# genuinely wrong password (or a submission that silently failed) is still
# reported within a few seconds, never retried silently forever.
_PASSWORD_CONFIRM_POLLS = 5
_FIELD_CONFIRM_POLLS = 15
_FIELD_CONFIRM_INTERVAL = 1.0


def config_dir() -> Path:
    override = os.environ.get("QA_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "qa-cli"


def qr_path() -> Path:
    return config_dir() / "telegram-qr.png"


def parse_login_timeout(text: str) -> float:
    """Seconds `login` waits for the scan: a finite number >= 0, where 0 means
    no deadline (a remote human may need longer than any fixed default)."""
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{text!r} is not a number of seconds >= 0")
    return value


class _QrHook:
    """Runs the operator's --qr-hook command with each freshly written QR PNG
    path appended, so an agent can forward every rotated QR to a remote human.

    The hook is never waited on while the login polls: a slow upload must not
    stall QR polling, and a hook that fails must not abort the login. Its output
    never reaches the terminal (it would scribble over the in-place QR view);
    stderr is kept in a temp file and quoted in the warning when the hook fails.
    A run's QR expires within a minute, so a run that outlives MAX_AGE, or is
    still going FINAL_GRACE after the login ended, is killed with its process
    group: forwarding a dead QR is pointless, and over a `--timeout 0` wait a
    hung run would otherwise hold a process and a growing stderr file forever.
    """

    # Telegram rotates the QR every ~20-30 s; a minute-old run is forwarding a dead one.
    MAX_AGE = 60.0
    # When the login ends, hooks still running get this long to finish so their
    # failures are reported; then they are killed.
    FINAL_GRACE = 5.0
    # Hung runs are killed only at MAX_AGE; until then, past this many unfinished
    # runs, new QRs skip the hook with a warning instead of stacking processes.
    MAX_RUNNING = 4

    def __init__(self, argv: Sequence[str]) -> None:
        self._argv = list(argv)
        self._running: list[tuple[subprocess.Popen, IO[bytes], float]] = []

    def fire(self, png: Path) -> None:
        self.reap()
        if len(self._running) >= self.MAX_RUNNING:
            print(
                f"warning: --qr-hook: {len(self._running)} earlier runs are still going; skipping this QR",
                file=sys.stderr,
            )
            return
        try:
            err = tempfile.TemporaryFile()
        except OSError as exc:
            print(f"warning: --qr-hook did not start: {exc}", file=sys.stderr)
            return
        try:
            proc = subprocess.Popen(
                [*self._argv, str(png)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err,
                start_new_session=True,
            )
        except OSError as exc:
            err.close()
            print(f"warning: --qr-hook did not start: {exc}", file=sys.stderr)
            return
        self._running.append((proc, err, time.monotonic()))

    def reap(self, final: bool = False) -> None:
        """Report finished runs and kill expired ones; `final` (the login ended)
        first gives every run FINAL_GRACE to finish, then kills the rest."""
        if not self._running:
            return
        now = time.monotonic()
        grace_end = now + self.FINAL_GRACE
        running = []
        for proc, err, started in self._running:
            try:
                code = proc.wait(max(0.0, grace_end - time.monotonic())) if final else proc.poll()
            except subprocess.TimeoutExpired:
                code = None
            if code is None and (final or now - started >= self.MAX_AGE):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                err.close()
                why = "was still running when the login ended" if final else f"ran over {self.MAX_AGE:.0f} s"
                print(f"warning: --qr-hook {why}; killed", file=sys.stderr)
                continue
            if code is None:
                running.append((proc, err, started))
                continue
            if code != 0:
                err.seek(max(0, err.seek(0, os.SEEK_END) - 300))
                detail = err.read().decode(errors="replace").strip()
                how = f"was killed by signal {-code}" if code < 0 else f"exited {code}"
                print(f"warning: --qr-hook {how}" + (f": {detail}" if detail else ""), file=sys.stderr)
            err.close()
        self._running = running


def render_qr_ascii(data: str) -> str:
    """Render `data` as a terminal-friendly Unicode QR code block."""
    qr = qrcode.QRCode(border=4)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, tty=False)
    return buf.getvalue()


def _describe_wait(waiting_form):
    if waiting_form in (None, AUTH_QR_FORM_ID):
        return "waiting for QR scan"
    if waiting_form == AUTH_PASSWORD_FORM_ID:
        return "waiting on the Telegram two-factor cloud password step (auth-password-form)"
    return (
        f"waiting on Telegram auth step '{waiting_form}' — this qa-cli flow "
        f"cannot complete it automatically; {_MANUAL_STEP_FALLBACK}"
    )


class _TerminalQrPainter:
    """Redraws a full status/QR block in place using an ANSI screen clear."""

    def __init__(self, out) -> None:
        self._out = out
        self._drawn = False
        self._tty = bool(getattr(out, "isatty", lambda: False)())
        if self._tty:
            self._out.write("\033[?1049h\033[?25l")

    def close(self) -> None:
        if self._tty:
            self._out.write("\033[?25h\033[?1049l")
            self._out.flush()

    def render(self, message: str, body: str = "") -> None:
        if self._drawn and self._tty:
            self._out.write("\033[2J\033[H")
        self._out.write(message + "\n")
        if body:
            self._out.write("\n" + body)
        self._out.flush()
        self._drawn = True


def _interactive_input_available():
    return bool(getattr(sys.stdin, 'isatty', lambda: False)())


def _secure_getpass(prompt):
    if not _interactive_input_available():
        raise AuthStepRequiredError('Interactive terminal required for auth-code-form/auth-password-form; re-run qa telegram login in a terminal')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt)


def _prompt_and_submit_password(browser: BrowserDriver, painter: _TerminalQrPainter) -> None:
    """Prompt for Telegram's cloud password in the terminal (masked) and
    submit it into the live page.

    The password is never logged, printed, written to a file, or included
    in any exception message -- it exists only as this function's local
    `password` variable until it is handed to `browser.submit_password`,
    which itself sends it to agent-browser over subprocess stdin, never argv.
    """
    painter.render(
        "Telegram requires the two-factor cloud password -- "
        "check this terminal for a masked password prompt."
    )
    try:
        password = _secure_getpass("Telegram cloud password (input hidden): ")
    except (EOFError, OSError, getpass.GetPassWarning) as exc:
        raise AuthStepRequiredError(
            "Telegram requires the two-factor cloud password, but this "
            "terminal has no interactive input to prompt for it; re-run "
            f"from an interactive terminal, or {_MANUAL_STEP_FALLBACK}"
        ) from exc
    if not password:
        raise AuthStepRequiredError(
            "Telegram requires the two-factor cloud password, but none "
            f"was entered; re-run and type the password, or {_MANUAL_STEP_FALLBACK}"
        )
    browser.submit_password(password)


def _confirm_password_step(browser: BrowserDriver, poll_interval: float) -> AuthStatus:
    """After `browser.submit_password`, poll until `auth-password-form`
    clears (success, or Telegram moved to a different next step) or it
    persists across `_PASSWORD_CONFIRM_POLLS` polls, in which case the
    password was rejected (or the submission silently failed).
    """
    for _ in range(_PASSWORD_CONFIRM_POLLS):
        time.sleep(poll_interval)
        status = browser.auth_status()
        if status.waiting_form != AUTH_PASSWORD_FORM_ID:
            return status
    raise AuthPasswordRejectedError(
        "Telegram rejected the password (or another step is required); re-run to try again"
    )

def _prompt_terminal_field(browser, painter, form, poll_interval=DEFAULT_POLL_INTERVAL):
    kind = 'code' if form == CODE_FORM else 'phone'
    prompt = 'Telegram verification code (5 digits, input hidden): ' if kind == 'code' else 'Telegram phone (+country code, input hidden): '
    painter.render('Enter the requested login value in this terminal; it is not logged.')
    try:
        value = _secure_getpass(prompt)
    except (EOFError, OSError, getpass.GetPassWarning):
        raise AuthStepRequiredError('Secure interactive terminal input is unavailable; re-run qa telegram login in a terminal') from None
    if browser.auth_status().waiting_form != form:
        raise AuthStepRequiredError('Telegram authentication step changed; no value submitted. Re-run login.')
    if kind == 'code':
        browser.submit_code(value.strip())
    else:
        browser.submit_phone(value.strip())
    for _ in range(_FIELD_CONFIRM_POLLS):
        time.sleep(min(_FIELD_CONFIRM_INTERVAL, max(0, poll_interval)))
        status = browser.auth_status()
        if status.waiting_form != form:
            return status
    raise AuthStepRequiredError('Telegram has not accepted this login step. Check the code/phone and retry; no automatic resubmission.')

def _run_qr_login_loop(
    browser: BrowserDriver,
    timeout: float,
    poll_interval: float,
    out,
    qr_hook: Optional[_QrHook] = None,
) -> None:
    dest = qr_path()
    # timeout 0: no deadline -- keep refreshing the QR until someone scans it.
    deadline = time.monotonic() + timeout if timeout else None
    painter = _TerminalQrPainter(out)
    try:
        last_rendered = None
        manual_form = None
        manual_polls = 0
        status: AuthStatus = browser.auth_status()
        while not status.logged_in:
            if deadline is not None and time.monotonic() >= deadline:
                dump = browser.diagnostic_dump()
                raise TimeoutError(
                    "timed out waiting for Telegram Web QR login "
                    f"({_describe_wait(status.waiting_form)}) [{dump}]"
                )
            if status.waiting_form in (None, AUTH_QR_FORM_ID):
                manual_form = None
                manual_polls = 0
                token = browser.get_qr_token()
                key = ("qr", token)
                if token and key != last_rendered:
                    with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".png", delete=False) as protected:
                        protected_path = Path(protected.name)
                    try:
                        browser.screenshot(str(protected_path))
                        protected_path.chmod(0o600)
                        protected_path.replace(dest)
                    finally:
                        protected_path.unlink(missing_ok=True)
                    painter.render(
                        f"{_describe_wait(status.waiting_form)} "
                        f"(auto-refreshes; also saved to {dest})",
                        render_qr_ascii(f"{_QR_URL_PREFIX}{token}"),
                    )
                    last_rendered = key
                    if qr_hook is not None:
                        qr_hook.fire(dest)
                elif not token and last_rendered is None:
                    painter.render("waiting for Telegram to generate a QR code…")
                    last_rendered = ("pending", None)
            else:
                manual_polls = manual_polls + 1 if manual_form == status.waiting_form else 1
                manual_form = status.waiting_form
                if manual_polls >= 2:
                    if status.waiting_form in (CODE_FORM, PHONE_FORM):
                        status = _prompt_terminal_field(browser, painter, status.waiting_form, poll_interval)
                        manual_form = None
                        manual_polls = 0
                        continue
                    if status.waiting_form != AUTH_PASSWORD_FORM_ID:
                        raise AuthStepRequiredError(_describe_wait(status.waiting_form))
                    _prompt_and_submit_password(browser, painter)
                    status = _confirm_password_step(browser, poll_interval)
                    manual_form = None
                    manual_polls = 0
                    continue
                painter.render("Telegram is changing authentication state…")
            time.sleep(poll_interval)
            status = browser.auth_status()
    finally:
        painter.close()
        if qr_hook is not None:
            qr_hook.reap(final=True)


def login(
    browser: BrowserDriver,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
    qr_timeout: float = DEFAULT_QR_WAIT_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    out=None,
    phone: bool = False,
    qr_hook: Optional[Sequence[str]] = None,
) -> int:
    """Log in to Telegram Web. `timeout` bounds the wait for the scan in
    seconds (0 = no deadline); `qr_hook` is an argv run with the QR PNG path
    appended every time a new QR is written."""
    directory = config_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not os.environ.get("QA_CONFIG_DIR"):
        directory.chmod(0o700)
    set_deadline = getattr(browser, "set_deadline", None)
    if set_deadline:
        set_deadline(time.monotonic() + timeout + qr_timeout if timeout else None)
    try:
        browser.open(TELEGRAM_WEB_URL)
        if not browser.is_logged_in():
            browser.wait_for_qr(qr_timeout)
            if phone and browser.auth_status().waiting_form == AUTH_QR_FORM_ID:
                browser.switch_to_phone()
            if not browser.is_logged_in():
                _run_qr_login_loop(
                    browser,
                    timeout,
                    poll_interval,
                    out if out is not None else sys.stdout,
                    _QrHook(qr_hook) if qr_hook else None,
                )
    finally:
        if set_deadline:
            set_deadline(None)
    target = out if out is not None else sys.stdout
    print("logged-in", file=target)
    print(f"session: {SESSION_NAME}", file=target)
    print(f"config: {config_dir()}", file=target)
    return 0


def status(browser: BrowserDriver) -> int:
    browser.open(TELEGRAM_WEB_URL)
    state = "logged-in" if browser.is_logged_in() else "logged-out"
    print(state)
    print(f"session: {SESSION_NAME}")
    print(f"config: {config_dir()}")
    return 0
