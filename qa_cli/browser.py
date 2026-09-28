"""Browser driver for Telegram Web via the agent-browser CLI.

Accessed via: `qa telegram login|status` -> AgentBrowserDriver (production)
or an injected BrowserDriver (hermetic tests).

Assumptions: default binary is `agent-browser` on PATH (override QA_BROWSER_BIN);
headless Chromium is agent-browser's default (no --headed); session name
qa-telegram with --restore so re-runs skip QR. QR-ready means a painted
QR-related canvas (non-uniform opaque pixels), a QR-labeled img, or a
QR-related svg (>= 80px) — a blank or unrelated canvas is not ready.
wait_for_qr also returns if a restored session becomes logged-in, or if any
OTHER auth form (e.g. a resumed session parked on auth-password-form) is
already mounted, so a re-run never dead-ends waiting for QR pixels that
will never appear.

Telegram Web A uses lazily loaded UI modules. Before login it renders
#auth-<step>-form inside .is-auth; after login the main module mounts #Main
and #LeftColumn. Missing auth forms alone do not prove login: blank pages,
loading, browser errors and transitional screens are treated as unknown.

get_qr_token() first decodes Telegram's already-rendered QR image using
Chromium's native BarcodeDetector. This captures the first frame without
waiting for rotation or altering the application. For browsers without this
API, an idempotent module hook observes future QR rotations; failed hook
initialization is retried after a bounded interval.

Never invokes macOS `open -a Telegram`, Playwright, Puppeteer, or npx.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import NamedTuple, Optional, Protocol

TELEGRAM_WEB_URL = "https://web.telegram.org/a/"
SESSION_NAME = "qa-telegram"
INSTALL_HINT = "npm i -g agent-browser && agent-browser install"
# The `#auth-<step>-form` DOM ids this driver treats specially. Every OTHER
# waiting_form value is an unsupported step this qa-cli flow escalates
# instead of automating. Defined once so a typo in a comparison fails an
# import-time NameError instead of silently mismatching a string literal.
AUTH_QR_FORM_ID = "auth-qr-form"
AUTH_PASSWORD_FORM_ID = "auth-password-form"
_SUBPROCESS_TIMEOUT_SECONDS = 10.0
_NAVIGATION_TIMEOUT_SECONDS = 45.0
_QR_CAPTURE_GRACE_SECONDS = 15.0


class AuthStatus(NamedTuple):
    """Result of probing Telegram Web A's current authorization state."""

    logged_in: bool
    # DOM id of the current `#auth-<step>-form`, e.g. "auth-qr-form" or
    # "auth-password-form"; None once logged in or before any form renders.
    waiting_form: Optional[str]


# Positive visible main-shell evidence is required. Hidden/stale auth forms,
# a blank page or a slow lazy module must not become a false authenticated state.
_IS_OFFICIAL_TELEGRAM_PAGE_JS = """(() => {
  try {
    const url = new URL(location.href);
    return url.origin === 'https://web.telegram.org' && url.pathname === '/a/'
      && !url.username && !url.password;
  } catch (_) { return false; }
})()"""

_AUTH_STATUS_JS = """(() => {
  if (!(""" + _IS_OFFICIAL_TELEGRAM_PAGE_JS + """)) return JSON.stringify({loggedIn: false, waitingForm: null});
  const visible = (el) => {
    if (!el || (el.getClientRects && !el.getClientRects().length)) return false;
    if (typeof getComputedStyle !== 'function') return true;
    for (let node = el; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || style.opacity === '0') return false;
    }
    return true;
  };
  const form = Array.from(document.querySelectorAll('[id^="auth-"][id$="-form"]')).find(visible);
  if (form) return JSON.stringify({loggedIn: false, waitingForm: form.id});
  if (visible(document.querySelector('.is-auth'))) return JSON.stringify({loggedIn: false, waitingForm: null});
  const shell = document.querySelector('#LeftColumn, #column-left, #Main, .chat-list, .ChatList, [data-testid="chat-list"]');
  return JSON.stringify({loggedIn: visible(shell), waitingForm: null});
})()"""

# Read the existing rendered QR immediately, without mutating Telegram's app.
# Native barcode decoding is not OCR. The module hook below is a fallback for
# browsers that do not implement BarcodeDetector (and waits for the next render).
_QR_IMAGE_TOKEN_JS = r"""(async () => {
  if (typeof BarcodeDetector !== 'function') return JSON.stringify({token: null, supported: false});
  let url;
  let sourceMarkup;
  try {
    const formats = await BarcodeDetector.getSupportedFormats();
    if (!formats.includes('qr_code')) return JSON.stringify({token: null, supported: false});
    let image = document.querySelector('.qr-container canvas, .qr-container img');
    const svg = document.querySelector('.qr-container svg');
    if (svg) {
      sourceMarkup = new XMLSerializer().serializeToString(svg);
      if (window.__qaQrImageCache?.markup === sourceMarkup) return JSON.stringify({token: window.__qaQrImageCache.token});
      url = URL.createObjectURL(new Blob([sourceMarkup], {type: 'image/svg+xml'}));
      const img = new Image(); img.src = url;
      await img.decode();
      const canvas = document.createElement('canvas');
      canvas.width = img.width; canvas.height = img.height;
      canvas.getContext('2d').drawImage(img, 0, 0); image = canvas;
    }
    if (!image) return JSON.stringify({token: null});
    const codes = await new BarcodeDetector({formats: ['qr_code']}).detect(image);
    const code = codes.find((item) => /^tg:\/\/login\?token=[A-Za-z0-9_-]+$/.test(item.rawValue));
    const token = code ? code.rawValue.split('token=')[1] : null;
    if (svg && token) window.__qaQrImageCache = {markup: sourceMarkup, token};
    return JSON.stringify({token});
  } catch (_) { return JSON.stringify({token: null}); }
  finally { if (url) URL.revokeObjectURL(url); }
})()"""

# Fallback only: observes Telegram's dynamically-imported
# qr-code-styling bundle by grepping the entry module's source for the
# literal specifier (stable across Vite content-hash rebuilds), then
# monkeypatches the shared QRCodeStyling.prototype.update so every future
# render/rotation records its `data` (a `tg://login?token=...` string) on
# window.__qaQrHook. Idempotent: a second call reuses the existing hook.
_QR_TOKEN_JS = (
    "(async () => {"
    "  const w = window;"
    "  if (!w.__qaQrHook || (w.__qaQrHook.error && Date.now() - w.__qaQrHook.at > 5000)) {"
    "    w.__qaQrHook = { token: null, at: Date.now() };"
    "    try {"
    "      const mainScript = Array.from("
    "        document.querySelectorAll('script[type=\"module\"][src]')"
    "      ).map((s) => s.src)[0];"
    "      if (!mainScript) throw new Error('no entry module script found');"
    "      const src = await (await fetch(mainScript)).text();"
    "      const match = src.match(/[\"']([^\"']*qr-code-styling[^\"']*\\.js)[\"']/);"
    "      if (!match) throw new Error('qr-code-styling specifier not found');"
    "      const qrUrl = new URL(match[1], mainScript).href;"
    "      const mod = await import(qrUrl);"
    "      const proto = mod && mod.default && mod.default.prototype;"
    "      if (!proto || typeof proto.update !== 'function') {"
    "        throw new Error('unexpected qr-code-styling module shape');"
    "      }"
    "      const orig = proto.update;"
    "      const prefix = 'tg://login?token=';"
    "      proto.update = function (opts) {"
    "        try {"
    "          if (opts && typeof opts.data === 'string' && opts.data.indexOf(prefix) === 0) {"
    "            w.__qaQrHook.token = opts.data.slice(prefix.length);"
    "          }"
    "        } catch (e) {}"
    "        return orig.apply(this, arguments);"
    "      };"
    "    } catch (e) {"
    "      w.__qaQrHook.error = String((e && e.message) || e);"
    "    }"
    "  }"
    "  return JSON.stringify(w.__qaQrHook);"
    "})()"
)

# Side-effect free: read only. Compact page state for a timeout error so a
# stuck login is debuggable from CLI output alone, without reopening a
# headed browser.
_DIAGNOSTIC_JS = """JSON.stringify({
  app: 'Telegram Web', url: location.origin + location.pathname,
  ready: document.readyState,
  authForm: (document.querySelector('[id^="auth-"][id$="-form"]') || {}).id || null
})"""

# Called only while auth_status().waiting_form == "auth-password-form". Sets
# Telegram's live #sign-in-password input via the native value setter (so
# the framework's change listener fires exactly as it would for real
# typing), then submits the form. `__QA_PASSWORD_JSON__` is a placeholder,
# string-replaced with a json.dumps(...) literal by submit_password() just
# before sending -- it is a valid, escaped JS string literal, never a raw
# string interpolation. Defensive: falls back from the known input id to
# any password input inside the form, and reports a distinct failure
# reason (never page content or the password) if the form or input is
# missing instead of silently doing nothing.
# Side-effect free: painted QR canvas, QR <img>, or QR-related <svg>.
# Telegram sizes a canvas before drawing; dimensions alone are not ready.
# Live Web A currently paints the login QR as SVG inside .qr-container.
_QR_VISIBLE_JS = (
    "(() => {"
    "  const qrHay = (el) => {"
    "    const cls = typeof el.className === 'string' ? el.className"
    "      : ((el.className && el.className.baseVal) || '');"
    "    const aria = el.getAttribute ? (el.getAttribute('aria-label') || '') : '';"
    "    return (cls + ' ' + (el.id || '') + ' ' + (el.alt || '') + ' '"
    "      + (el.src || '') + ' ' + aria).toLowerCase();"
    "  };"
    "  const isQrRelated = (el) => {"
    "    let n = el;"
    "    for (let i = 0; i < 8 && n; i++) {"
    "      if (/qr/.test(qrHay(n))) return true;"
    "      n = n.parentElement;"
    "    }"
    "    return false;"
    "  };"
    "  const isPainted = (c) => {"
    "    try {"
    "      const ctx = c.getContext && c.getContext('2d');"
    "      if (!ctx || !ctx.getImageData) return false;"
    "      const w = c.width || 0, h = c.height || 0;"
    "      if (w < 1 || h < 1) return false;"
    "      const d = ctx.getImageData(0, 0, w, h).data;"
    "      let first = -1;"
    "      for (let i = 0; i < d.length; i += 4) {"
    "        if (d[i + 3] < 16) continue;"
    "        const key = (d[i] << 16) | (d[i + 1] << 8) | d[i + 2];"
    "        if (first < 0) first = key;"
    "        else if (key !== first) return true;"
    "      }"
    "      return false;"
    "    } catch (e) { return false; }"
    "  };"
    "  const svgSize = (s) => {"
    "    const attrW = parseFloat(s.getAttribute && s.getAttribute('width')) || 0;"
    "    const attrH = parseFloat(s.getAttribute && s.getAttribute('height')) || 0;"
    "    const baseW = (s.width && s.width.baseVal && s.width.baseVal.value) || attrW;"
    "    const baseH = (s.height && s.height.baseVal && s.height.baseVal.value) || attrH;"
    "    let vbW = 0, vbH = 0;"
    "    const vb = s.getAttribute && s.getAttribute('viewBox');"
    "    if (vb) {"
    "      const p = vb.trim().split(/\\s+/);"
    "      if (p.length === 4) { vbW = parseFloat(p[2]) || 0; vbH = parseFloat(p[3]) || 0; }"
    "    }"
    "    return {"
    "      w: Math.max(baseW, vbW, s.clientWidth || 0),"
    "      h: Math.max(baseH, vbH, s.clientHeight || 0)"
    "    };"
    "  };"
    "  const qrSvg = document.querySelector('.qr-container svg');"
    "  if (qrSvg) {"
    "    const { w, h } = svgSize(qrSvg);"
    "    if (w >= 80 && h >= 80) return 'qr-visible';"
    "  }"
    "  const canvases = Array.from(document.querySelectorAll('canvas'));"
    "  for (const c of canvases) {"
    "    const w = Math.max(c.width || 0, c.clientWidth || 0);"
    "    const h = Math.max(c.height || 0, c.clientHeight || 0);"
    "    if (w >= 80 && h >= 80 && isQrRelated(c) && isPainted(c)) return 'qr-visible';"
    "  }"
    "  const imgs = Array.from(document.querySelectorAll('img'));"
    "  for (const img of imgs) {"
    "    const w = Math.max(img.naturalWidth || 0, img.clientWidth || 0);"
    "    const h = Math.max(img.naturalHeight || 0, img.clientHeight || 0);"
    "    if (isQrRelated(img) && w >= 80 && h >= 80) return 'qr-visible';"
    "  }"
    "  const svgs = Array.from(document.querySelectorAll('svg'));"
    "  for (const s of svgs) {"
    "    const { w, h } = svgSize(s);"
    "    if (isQrRelated(s) && w >= 80 && h >= 80) return 'qr-visible';"
    "  }"
    "  return 'qr-missing';"
    "})()"
)


class BrowserError(RuntimeError):
    """agent-browser ran but failed (nonzero exit)."""


class AuthStepRequiredError(BrowserError):
    """A password or other manual auth step is required; do not keep polling QR."""


class AuthPasswordRejectedError(BrowserError):
    """The submitted cloud password was rejected (or Telegram now wants
    yet another step); do not keep retrying automatically."""


class BrowserMissingError(FileNotFoundError):
    """Raised when the agent-browser binary is not on PATH."""

    def __init__(self, binary: str) -> None:
        super().__init__(binary)
        self.binary = binary


class BrowserDriver(Protocol):
    def set_deadline(self, deadline: Optional[float]) -> None: ...
    def open(self, url: str) -> None: ...
    def screenshot(self, path: str) -> None: ...
    def is_logged_in(self) -> bool: ...
    def wait_for_qr(self, timeout: float) -> None: ...
    def auth_status(self) -> AuthStatus: ...
    def get_qr_token(self) -> Optional[str]: ...
    def diagnostic_dump(self) -> str: ...
    def submit_password(self, password: str) -> None: ...
    def submit_code(self, code: str) -> None: ...
    def submit_phone(self, phone: str) -> None: ...
    def switch_to_phone(self) -> None: ...


def browser_bin() -> str:
    return os.environ.get("QA_BROWSER_BIN") or "agent-browser"


def _coerce_token(value) -> Optional[str]:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,1024}", value) else None


class AgentBrowserDriver:
    """Drives Telegram Web through the agent-browser CLI (stdlib subprocess)."""

    def __init__(self, binary: Optional[str] = None) -> None:
        self.binary = binary or browser_bin()
        self._qr_failure_since: Optional[float] = None
        self._image_decoder_supported = True
        self._deadline: Optional[float] = None

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

    def _resolve_timeout(self, base: float) -> float:
        if self._deadline is None:
            return base
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Telegram login deadline exceeded")
        return min(base, remaining)

    def _invoke(
        self, argv: list[str], timeout: float, input_text: Optional[str] = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                argv, input=input_text, check=False, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise BrowserError(f"agent-browser command timed out after {timeout:g}s") from None
        except FileNotFoundError as exc:
            raise BrowserMissingError(self.binary) from exc
        if proc.returncode != 0:
            diagnostic = (proc.stderr or proc.stdout or "").lower()
            category = "command_failed"
            for phrase, label in (("unknown option", "unsupported_cli_option"), ("permission denied", "permission_denied"), ("browser not found", "browser_missing"), ("connection refused", "browser_unreachable")):
                if phrase in diagnostic:
                    category = label
                    break
            raise BrowserError(f"agent-browser {category} (exit {proc.returncode}); inspect locally, no page data logged")
        return proc

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = self.build_argv(*args)
        base_timeout = _NAVIGATION_TIMEOUT_SECONDS if args and args[0] == "open" else _SUBPROCESS_TIMEOUT_SECONDS
        timeout = self._resolve_timeout(base_timeout)
        return self._invoke(argv, timeout)

    def _run_stdin(self, *args: str, input_text: str) -> subprocess.CompletedProcess[str]:
        """Like `_run`, but pipes `input_text` to the agent-browser
        subprocess's stdin instead of appending it to argv, so a live
        secret (e.g. a typed 2FA password) never appears in argv -- visible
        to any other local process via `ps`/`/proc`. `_invoke`'s error
        categorization only ever inspects stderr/stdout phrasing, never
        `input_text`, so a failure here cannot leak the secret either.
        """
        argv = self.build_argv(*args)
        timeout = self._resolve_timeout(_SUBPROCESS_TIMEOUT_SECONDS)
        return self._invoke(argv, timeout, input_text=input_text)

    def set_deadline(self, deadline: Optional[float]) -> None:
        self._deadline = deadline

    def open(self, url: str) -> None:
        self._image_decoder_supported = True
        self._qr_failure_since = None
        # A status check must not reload a selected chat or restart lazy hydration.
        # Authentication probes also check the origin, including after navigation.
        if url == TELEGRAM_WEB_URL and self._eval_json(_IS_OFFICIAL_TELEGRAM_PAGE_JS) is True:
            return
        self._run("open", url)

    def screenshot(self, path: str) -> None:
        self._run("screenshot", path)

    @staticmethod
    def _decode_eval_result(raw: str):
        """Decode agent-browser's JSON.stringify(...) return value.

        agent-browser prints a JS string return value JSON-encoded, so a
        `JSON.stringify(...)` payload arrives double-encoded (once from
        agent-browser's own quoting, once from our script).
        """
        try:
            outer = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(outer, str):
            return outer
        try:
            return json.loads(outer)
        except (json.JSONDecodeError, ValueError):
            return None

    def _eval_json(self, js: str):
        """Run `js` (passed via argv) and decode its return value."""
        proc = self._run("eval", js)
        return self._decode_eval_result((proc.stdout or "").strip())

    def _eval_json_stdin(self, js: str):
        """Run `js` (piped via stdin, see `_run_stdin`) and decode its
        return value. Use for any JS that embeds a live secret."""
        proc = self._run_stdin("eval", "--stdin", input_text=js)
        return self._decode_eval_result((proc.stdout or "").strip())

    def auth_status(self) -> AuthStatus:
        data = self._eval_json(_AUTH_STATUS_JS)
        if not isinstance(data, dict):
            return AuthStatus(logged_in=False, waiting_form=None)
        waiting_form = data.get("waitingForm")
        return AuthStatus(
            logged_in=data.get("loggedIn") is True,
            waiting_form=waiting_form if isinstance(waiting_form, str) else None,
        )

    def is_logged_in(self) -> bool:
        return self.auth_status().logged_in

    def get_qr_token(self) -> Optional[str]:
        if self._image_decoder_supported:
            image_result = self._eval_json(_QR_IMAGE_TOKEN_JS)
            if isinstance(image_result, dict):
                if image_result.get("supported") is False:
                    self._image_decoder_supported = False
                token = _coerce_token(image_result.get("token"))
                if token:
                    self._qr_failure_since = None
                    return token
        hook_result = self._eval_json(_QR_TOKEN_JS)
        if not isinstance(hook_result, dict):
            return None
        if hook_result.get("error"):
            now = time.monotonic()
            if self._qr_failure_since is None:
                self._qr_failure_since = now
            # The JS fallback retries after 5s. Allow three retry intervals.
            if now - self._qr_failure_since >= _QR_CAPTURE_GRACE_SECONDS:
                raise BrowserError("Telegram QR capture is unavailable after retries; update agent-browser and retry")
            return None
        self._qr_failure_since = None
        return _coerce_token(hook_result.get("token"))

    def diagnostic_dump(self) -> str:
        data = self._eval_json(_DIAGNOSTIC_JS)
        if not isinstance(data, dict):
            return "diagnostic unavailable (eval failed)"
        return (
            f"app={data.get('app', 'Telegram Web')!r} "
            f"url={data.get('url', '')!r} "
            f"ready={data.get('ready', 'unknown')!r} form={data.get('authForm')!r}"
        )

    def _auth_batch(self, commands) -> None:
        proc = self._run_stdin('batch', '--bail', '--json', input_text=json.dumps(commands))
        try:
            results = json.loads(proc.stdout)
        except (ValueError, TypeError):
            raise BrowserError('Telegram auth command returned an unreadable response') from None
        if not isinstance(results, list) or len(results) != len(commands) or any(
            not isinstance(item, dict) or item.get('success') is not True for item in results
        ):
            raise BrowserError('Telegram auth input was not accepted; no secret included in diagnostics')

    def _submit_terminal_field(self, kind, value) -> None:
        """Secrets stay in stdin/memory; never argv, file logs or raw errors."""
        from qa_cli.auth_input import submission_commands
        try:
            commands = submission_commands(kind, value)
        except ValueError as error:
            raise AuthStepRequiredError(str(error)) from None
        self._auth_batch(commands)

    def submit_code(self, code: str) -> None:
        self._submit_terminal_field('code', code)

    def submit_phone(self, phone: str) -> None:
        self._submit_terminal_field('phone', phone)

    def submit_password(self, password: str) -> None:
        self._submit_terminal_field('password', password)

    def switch_to_phone(self) -> None:
        from qa_cli.auth_input import phone_login_commands
        self._auth_batch(phone_login_commands())

    def is_qr_visible(self) -> bool:
        proc = self._run("eval", _QR_VISIBLE_JS)
        out = (proc.stdout or "").strip().strip('"').strip("'")
        return out.endswith("qr-visible") or out == "qr-visible"

    def wait_for_qr(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.auth_status()
            # Any mounted auth form OTHER than the QR form (e.g. a persisted
            # session resuming straight into auth-password-form) is just as
            # "ready" as a visible QR: the caller's login loop reacts to it
            # immediately. Waiting here for QR pixels specifically would
            # dead-end for 60s on a page that will never show one.
            if (
                status.logged_in
                or status.waiting_form not in (None, AUTH_QR_FORM_ID)
                or self.is_qr_visible()
            ):
                return
            time.sleep(0.5)
        raise TimeoutError("timed out waiting for Telegram Web QR code")
