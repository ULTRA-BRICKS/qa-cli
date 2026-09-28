"""Session restoration regressions; all pages and credentials are synthetic."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa_cli.browser import AgentBrowserDriver, AuthStatus, BrowserError, _AUTH_STATUS_JS
from qa_cli.cli import main

OFFICIAL = 'https://web.telegram.org/a/'

class SequenceBrowser:
    def __init__(self, states):
        self.states = list(states)
        self.last = self.states[-1]
        self.deadlines = []
    def open(self, url):
        assert url == OFFICIAL
    def set_deadline(self, value):
        self.deadlines.append(value)
    def auth_status(self):
        value = self.states.pop(0) if self.states else self.last
        if isinstance(value, Exception):
            raise value
        return value
    def is_logged_in(self):
        return self.auth_status().logged_in

@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr('qa_cli.telegram.time.monotonic', lambda: now[0])
    monkeypatch.setattr('qa_cli.telegram.time.sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    return now


def test_loading_then_authenticated_is_not_logout(clock, capsys):
    browser = SequenceBrowser([AuthStatus(False, None), AuthStatus(False, None), AuthStatus(True, None)])
    assert main(['telegram', 'status'], browser=browser) == 0
    output = capsys.readouterr()
    assert output.out.splitlines()[0] == 'logged-in'
    assert 'logged-out' not in output.out
    assert browser.deadlines[-1] is None
    assert 0 < clock[0] < 15


@pytest.mark.parametrize('form', ['auth-qr-form', 'auth-phone-number-form', 'auth-code-form', 'auth-password-form'])
def test_visible_auth_form_is_actual_logout(clock, capsys, form):
    browser = SequenceBrowser([AuthStatus(False, None), AuthStatus(False, form)])
    assert main(['telegram', 'status'], browser=browser) == 0
    assert capsys.readouterr().out.splitlines()[0] == 'logged-out'
    assert clock[0] > 0


def test_never_ready_is_unknown_not_logout(clock, capsys):
    assert main(['telegram', 'status'], browser=SequenceBrowser([AuthStatus(False, None)])) == 1
    output = capsys.readouterr()
    assert output.out.splitlines()[0] == 'unknown'
    assert 'logged-out' not in output.out
    assert 'scan' not in (output.out + output.err).lower()
    assert 0 < clock[0] <= 15.001


def test_browser_failure_is_not_logout_and_clears_deadline(clock, capsys):
    browser = SequenceBrowser([BrowserError('browser unavailable')])
    assert main(['telegram', 'status'], browser=browser) == 1
    assert 'logged-out' not in capsys.readouterr().out
    assert browser.deadlines[-1] is None


def test_unknown_state_clears_deadline(clock, capsys):
    browser = SequenceBrowser([AuthStatus(False, None)])
    main(['telegram', 'status'], browser=browser)
    assert browser.deadlines[-1] is None


def page_driver(monkeypatch, url):
    """Run the actual page-identity JavaScript against a synthetic URL."""
    page = {'url': url, 'navigations': 0}
    driver = AgentBrowserDriver(binary='synthetic-browser')
    def run(*args):
        if args[0] == 'eval':
            program = 'global.location = new URL(' + json.dumps(page['url']) + '); console.log(JSON.stringify(eval(' + json.dumps(args[1]) + ')));'
            output = subprocess.check_output(['node', '-e', program], text=True)
            return subprocess.CompletedProcess(args, 0, output, '')
        assert args[0] == 'open'
        page['url'], page['navigations'] = args[1], page['navigations'] + 1
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(driver, '_run', run)
    return driver, page


def test_open_retains_existing_official_chat(monkeypatch):
    selected = OFFICIAL + '#123456789'
    driver, page = page_driver(monkeypatch, selected)
    driver.open(OFFICIAL)
    assert page == {'url': selected, 'navigations': 0}


@pytest.mark.parametrize('url', [
    'about:blank', 'https://example.org/a/', 'https://web.telegram.org.evil.example/a/',
    'https://web.telegram.org/k/', 'http://web.telegram.org/a/',
    'https://web.telegram.org:9443/a/', 'https://user:pass@web.telegram.org/a/',
])
def test_nonofficial_page_is_not_reused(monkeypatch, url):
    driver, page = page_driver(monkeypatch, url)
    driver.open(OFFICIAL)
    assert page == {'url': OFFICIAL, 'navigations': 1}


@pytest.mark.parametrize('url', ['https://example.org/a/', 'https://web.telegram.org/k/', 'https://user:pass@web.telegram.org/a/'])
def test_fake_shell_on_wrong_page_cannot_authenticate(url):
    program = 'global.location = new URL(' + json.dumps(url) + ');'
    program += "global.document={querySelectorAll:()=>[],querySelector:s=>s.includes('#Main')?{getClientRects:()=>[1]}:null};"
    program += 'console.log(' + _AUTH_STATUS_JS + ');'
    output = subprocess.check_output(['node', '-e', program], text=True)
    assert json.loads(output)['loggedIn'] is False


def test_status_deadline_timeout_does_not_request_new_qr(clock, capsys):
    browser = SequenceBrowser([TimeoutError('Telegram login deadline exceeded')])
    assert main(['telegram', 'status'], browser=browser) == 1
    output = capsys.readouterr()
    assert output.out.splitlines()[0] == 'unknown'
    assert 'scan' not in (output.out + output.err).lower()
    assert browser.deadlines[-1] is None


def test_navigation_time_counts_towards_status_deadline(clock, capsys):
    browser = SequenceBrowser([AuthStatus(False, None)])
    def delayed_open(url):
        assert url == OFFICIAL
        clock[0] += 12
    browser.open = delayed_open
    assert main(['telegram', 'status'], browser=browser) == 1
    assert clock[0] <= 15.001
    assert capsys.readouterr().out.splitlines()[0] == 'unknown'


def test_login_retains_already_authenticated_chat(monkeypatch, tmp_path, capsys):
    selected = OFFICIAL + '#987654321'
    driver, page = page_driver(monkeypatch, selected)
    monkeypatch.setenv('QA_CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(driver, 'is_logged_in', lambda: True)
    assert main(['telegram', 'login'], browser=driver) == 0
    assert page == {'url': selected, 'navigations': 0}
    assert capsys.readouterr().out.splitlines()[0] == 'logged-in'


def test_unknown_status_bounds_browser_probe_count(clock, capsys):
    browser = SequenceBrowser([AuthStatus(False, None)])
    original = browser.auth_status
    probes = []
    def counted_status():
        probes.append(clock[0])
        return original()
    browser.auth_status = counted_status
    assert main(['telegram', 'status'], browser=browser) == 1
    assert len(probes) <= 31
    assert capsys.readouterr().out.splitlines()[0] == 'unknown'
