"""Native commands for the user's explicit terminal login; secrets use stdin."""
from __future__ import annotations
import json
import re

CODE_FORM = 'auth-code-form'
PHONE_FORM = 'auth-phone-number-form'
PASSWORD_FORM = 'auth-password-form'


def form_guard(form: str) -> str:
    return """(() => {
      if(location.origin !== 'https://web.telegram.org' || !location.pathname.startsWith('/a/'))
        throw new Error('unexpected-auth-origin');
      const form=document.getElementById(FORM);
      if(!form || !form.getClientRects().length || getComputedStyle(form).visibility==='hidden')
        throw new Error('auth-form-unavailable');
      return true;
    })()""".replace('FORM', json.dumps(form))

def wait_for_ready(selector: str) -> list[str]:
    target = json.dumps(selector)
    ready = f"(() => {{const e=document.querySelector({target});return location.origin==='https://web.telegram.org' && !!e && !!e.getClientRects().length && !e.disabled && e.getAttribute('aria-disabled')!=='true';}})()"
    return ['wait', '--fn', ready]


def phone_login_commands() -> list[list[str]]:
    selector = '#auth-qr-form .auth-form > button.auth-button:first-of-type'
    return [['eval', form_guard('auth-qr-form')], wait_for_ready(selector),
            ['click', selector], wait_for_ready('#auth-phone-number-form')]


def submission_commands(kind: str, value: str) -> list[list[str]]:
    if kind == 'code':
        if not re.fullmatch(r'[0-9]{5}', value):
            raise ValueError('Telegram Web A requires a five-digit verification code')
        form, selector = CODE_FORM, '#sign-in-code'
    elif kind == 'phone':
        value = re.sub(r'[ ()-]', '', value)
        if not re.fullmatch(r'\+[0-9]{7,15}', value):
            raise ValueError('Enter the phone number with +country code and digits only')
        form, selector = PHONE_FORM, '#sign-in-phone-number'
    elif kind == 'password':
        if not value or len(value) > 4096:
            raise ValueError('A nonempty cloud password is required')
        form, selector = PASSWORD_FORM, ':is(#sign-in-password,input[type="password"])'
    else:
        raise ValueError('Unsupported terminal authentication field')
    commands = [['eval', form_guard(form)], ['fill', f'#{form} {selector}', value]]
    if kind != 'code':
        button = f'#{form} button[type="submit"]'
        commands.append(wait_for_ready(button))
        commands.append(['click', button])
    return commands
