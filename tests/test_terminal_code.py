import io
import subprocess
import pytest
from qa_cli import telegram
from qa_cli.browser import AgentBrowserDriver, AuthStatus, AuthStepRequiredError

class CodeFlow:
    def __init__(self, password=False):
        self.code = None
        self.password = None
        self.needs_password = password
    def auth_status(self):
        if self.code is None: return AuthStatus(False, 'auth-code-form')
        if self.needs_password and self.password is None: return AuthStatus(False, 'auth-password-form')
        return AuthStatus(True, None)
    def submit_code(self, code): self.code = code
    def submit_password(self, password): self.password = password
    def diagnostic_dump(self): return 'sanitized'

def test_terminal_code_completes_without_browser_input(tmp_path, monkeypatch):
    monkeypatch.setenv('QA_CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(telegram.getpass, 'getpass', lambda *_args, **_kwargs: '12345')
    monkeypatch.setattr("qa_cli.telegram._interactive_input_available", lambda: True)
    driver = CodeFlow()
    out = io.StringIO()
    telegram._run_qr_login_loop(driver, 30, 0, out)
    assert driver.code == '12345'
    assert '12345' not in out.getvalue()

def test_code_then_cloud_password_are_two_distinct_steps(tmp_path, monkeypatch):
    monkeypatch.setenv('QA_CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(telegram, '_interactive_input_available', lambda: True)
    values = iter(['12345', 'synthetic-password'])
    monkeypatch.setattr(telegram.getpass, 'getpass', lambda *_args, **_kwargs: next(values))
    monkeypatch.setattr(telegram.time, 'sleep', lambda _seconds: None)
    driver = CodeFlow(password=True)
    output = io.StringIO()
    telegram._run_qr_login_loop(driver, 30, 0, output)
    assert driver.code == '12345' and driver.password == 'synthetic-password'
    assert 'synthetic-password' not in output.getvalue()

def test_no_terminal_refuses_before_reading_a_secret(monkeypatch):
    monkeypatch.setattr(telegram, '_interactive_input_available', lambda: False)
    monkeypatch.setattr(telegram.getpass, 'getpass', lambda *_a: pytest.fail('must not prompt'))
    with pytest.raises(AuthStepRequiredError, match='Interactive terminal'):
        telegram._secure_getpass('code: ')

@pytest.mark.parametrize('code', ['', '12', '123456', 'abcde', '１２３４５'])
def test_invalid_code_is_not_sent_to_the_browser(code, monkeypatch):
    driver = AgentBrowserDriver()
    monkeypatch.setattr(driver, '_auth_batch', lambda _script: pytest.fail('must not send'))
    with pytest.raises(AuthStepRequiredError, match='five-digit'):
        driver.submit_code(code)

def test_code_uses_stdin_not_process_arguments(monkeypatch):
    captured = {}
    def run(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, '[{"success":true},{"success":true}]', '')
    monkeypatch.setattr(subprocess, 'run', run)
    AgentBrowserDriver().submit_code('12345')
    assert '12345' not in ' '.join(captured['argv'])
    assert 'batch' in captured['argv'] and '--bail' in captured['argv']
    assert '12345' in captured['input']
    assert "location.origin" in captured['input']

def test_phone_option_selects_terminal_flow(monkeypatch):
    from qa_cli.cli import main
    seen = []
    monkeypatch.setattr(telegram, 'login', lambda _driver, **kwargs: seen.append(kwargs) or 0)
    assert main(['telegram', 'login', '--phone'], browser=CodeFlow()) == 0
    assert seen == [{'phone': True}]

def test_secure_getpass_rejects_echo_fallback(monkeypatch):
    import warnings
    monkeypatch.setattr(telegram, '_interactive_input_available', lambda: True)
    def unsafe(*_args): warnings.warn('cannot hide input', telegram.getpass.GetPassWarning)
    monkeypatch.setattr(telegram.getpass, 'getpass', unsafe)
    with pytest.raises(telegram.getpass.GetPassWarning): telegram._secure_getpass('code: ')

@pytest.mark.parametrize('kind,selector', [('code','#sign-in-code'),('phone','#sign-in-phone-number'),('password','#sign-in-password')])
def test_native_batch_has_guard_before_field_and_one_submit_path(kind, selector):
    from qa_cli.auth_input import submission_commands
    values = {'code':'12345','phone':'+12345678901','password':'synthetic-test-value'}
    commands = submission_commands(kind, values[kind])
    assert commands[0][0] == 'eval'
    assert 'web.telegram.org' in commands[0][1]
    assert commands[1][0] == 'fill' and selector in commands[1][1]
    assert len(commands) == (2 if kind == 'code' else 4)
    if kind != 'code':
        assert commands[2][:2] == ['wait', '--fn']
        assert commands[3][0] == 'click'

@pytest.mark.parametrize('output', ['not-json', '[]', '[{"success":false,"error":"private-test-detail"}]'])
def test_failed_batch_never_prints_raw_details(output, monkeypatch):
    from qa_cli.browser import BrowserError
    driver=AgentBrowserDriver()
    monkeypatch.setattr(driver,'_run_stdin',lambda *a, **k: subprocess.CompletedProcess([],0,output,''))
    with pytest.raises(BrowserError) as caught:
        driver.submit_code('12345')
    assert 'private-test-detail' not in str(caught.value)
    assert '12345' not in str(caught.value)


def test_phone_input_accepts_common_spacing_without_changing_digits():
    from qa_cli.auth_input import submission_commands
    commands = submission_commands('phone', '+1 (234) 567-8901')
    assert commands[1][2] == '+12345678901'

def test_cloud_password_field_preserves_type_fallback():
    from qa_cli.auth_input import submission_commands
    assert 'input[type="password"]' in submission_commands('password', 'synthetic')[1][1]

def test_switch_waits_for_button_and_confirms_phone_form_transition():
    from qa_cli.auth_input import phone_login_commands
    commands=phone_login_commands()
    assert [c[0] for c in commands]==['eval','wait','click','wait']
    assert '#auth-phone-number-form' in commands[-1][-1]
