"""Telegram Web A chat automation for bot end-to-end runs.

Accessed via: `qa telegram chat open|read|send|send-file|click|wait|screenshot`
(qa_cli.cli) and TelegramChat (library use by e2e harnesses, e.g. tg-cli's
scripts/e2e-telegram.ts, which shells out to the CLI and parses its JSON).

Assumptions:
- The account is logged in via `qa telegram login` (agent-browser restore key
  `qa-telegram`). Chat automation runs in its OWN agent-browser session
  (CHAT_SESSION_NAME) restored from that key with restore-save disabled:
  `qa telegram status|login` re-navigate the login session, which would close
  a chat mid-run, and this session must never write its state back over the
  login key.
- Writes (send, send-file, click) go ONLY to the explicit peer, and refuse
  unless that chat's header identifies a bot (override: allow_non_bot).
  Typing (send, send-file) also refuses unless the composer is empty -- a
  non-empty composer is the account owner's cloud-synced draft, and typing
  would append to it and send it.
- Message ids are the CLIENT's ids. In a private chat Telegram numbers
  messages per account, so a bot sees different message_id values for the
  same messages; correlate across sides by content markers, never by id.
- Opening a chat marks its visible messages read for the whole account.
- Text matching is NFKC-normalized: bots often style names with Unicode
  math alphanumerics (e.g. a bold window name) that NFKC folds to ASCII.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Sequence

from qa_cli.browser import TELEGRAM_WEB_URL, BrowserError, BrowserMissingError

CHAT_SESSION_NAME = "qa-telegram-chat"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
DEFAULT_WAIT_TIMEOUT = 60.0
_OPEN_TIMEOUT = 30.0
_CONFIRM_TIMEOUT = 30.0
_POLL_INTERVAL = 0.7
# Chat-header subtitle Telegram Web A shows for a bot, per UI language.
_BOT_SUBTITLES = frozenset({"bot", "бот"})
_COMPOSER = "#editable-message-text"
_SEARCH_INPUT = "#telegram-search-input"


class ChatError(BrowserError):
    """A chat action could not be completed (exit 1)."""


class ChatGuardError(ChatError):
    """A write was refused for safety: non-bot peer or pending draft (exit 2)."""


class ChatTimeoutError(ChatError):
    """The awaited message did not appear in time (exit 1)."""


class NotLoggedInError(ChatError):
    """Telegram Web shows an auth form instead of the chat shell (exit 3)."""


class ChatDriver(Protocol):
    def open(self, url: str) -> None: ...

    def eval_json(self, js: str): ...

    def eval_json_stdin(self, js: str): ...

    def batch(self, commands: Sequence[Sequence[str]]) -> None: ...

    def screenshot(self, path: str) -> None: ...


def normalize(text: str) -> str:
    """NFKC-fold and collapse whitespace so styled text compares as plain."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


@dataclass(frozen=True)
class ChatMessage:
    id: int
    out: bool
    text: str
    kind: str = "text"
    status: Optional[str] = None
    buttons: List[str] = field(default_factory=list)
    reactions: List[str] = field(default_factory=list)
    reply_to_text: Optional[str] = None

    def contains(self, needle: str) -> bool:
        return normalize(needle) in normalize(self.text)

    def to_json(self) -> dict:
        return asdict(self)


def _js(template: str, **values) -> str:
    """Substitute __NAME__ placeholders with JSON literals (never raw text).

    One pass: a value that itself contains a placeholder (a file named
    "x__MIME__.ogg") is never substituted again."""
    literals = {name: json.dumps(value) for name, value in values.items()}
    return re.sub(
        r"__([A-Z0-9]+)__",
        lambda m: literals.get(m.group(1), m.group(0)),
        template,
    )


# Rendered AND shown: Web A keeps hidden controls (e.g. the "Go to bottom"
# button) in the layout under an opacity:0 / pointer-events:none ancestor.
_VIS = """const vis = (e) => {
    if (!e || !e.getClientRects().length) return false;
    for (let n = e; n && n.nodeType === 1; n = n.parentElement) {
      const s = getComputedStyle(n);
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    }
    return true;
  };"""

# Inline keyboard buttons: Web A renders them (CSS-module class names) next to,
# not inside, .message-content; buttons inside it are media/player controls.
# Reactions shown outside the bubble (.Reactions.is-outside) are buttons there too, not keys.
_KEYBOARD = """const keyboard = (m) => Array.from(m.querySelectorAll('.message-content-wrapper button'))
    .filter((b) => !b.closest('.message-content') && !b.closest('.quick-reaction') && !b.closest('.Reactions'));"""

# Web A's START button for a bot chat this account never started; it replaces the
# composer in the chat footer. Labels per UI language.
_START_BUTTON = """const startButton = () => Array.from(document.querySelectorAll('#MiddleColumn .middle-column-footer button'))
    .find((b) => vis(b) && /^(start|старт|запустить|начать)$/i.test((b.textContent || '').trim()));"""

# The header's own title and status elements: with an initials avatar (no photo) the
# avatar letters are the first line of the header's text, so its lines can't be trusted.
_STATE_JS = """(() => {
  VIS
  START_BUTTON
  const onWeb = location.origin === 'https://web.telegram.org' && location.pathname.startsWith('/a/');
  const authForm = Array.from(document.querySelectorAll('[id^="auth-"][id$="-form"]')).some(vis);
  const shell = vis(document.querySelector('#LeftColumn, #Main'));
  const header = document.querySelector('.MiddleHeader');
  const text = (sel) => {
    const e = vis(header) ? header.querySelector(sel) : null;
    return e ? (e.textContent || '').trim() || null : null;
  };
  const title = text('.ChatInfo .info .title');
  const subtitle = text('.ChatInfo .info .status');
  const composer = document.querySelector('#editable-message-text');
  const bottom = Array.from(document.querySelectorAll('#MiddleColumn button')).some(
    (b) => vis(b) && /bottom/i.test(b.getAttribute('aria-label') || ''));
  return JSON.stringify({
    onWeb, authForm, loggedIn: shell && !authForm,
    open: location.hash === '#' + __PEER__ && title !== null,
    title, subtitle,
    composer: vis(composer), startButton: !!startButton(), draft: composer ? composer.textContent.length : 0,
    bottomButton: bottom,
  });
})()""".replace("VIS", _VIS).replace("START_BUTTON", _START_BUTTON)

# Tag (data-qa-target) the element a real click should hit: the peer's chat
# list entry, else its search result (never a message search hit, which
# would jump to an old message instead of opening the chat at its end).
_TAG_PEER_JS = """(() => {
  VIS
  document.querySelectorAll('[data-qa-target]').forEach((e) => e.removeAttribute('data-qa-target'));
  // Hit-testable only: with bot topics on, a forum panel covers the chat list
  // and its own "All Messages" entry is the peer anchor to click.
  const hittable = (e) => {
    if (!vis(e)) return false;
    const r = e.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    return !!hit && (hit === e || e.contains(hit));
  };
  const anchor = Array.from(document.querySelectorAll('#LeftColumn a[href="#' + __PEER__ + '"]')).find(hittable);
  if (anchor) { anchor.setAttribute('data-qa-target', 'peer'); return JSON.stringify({found: 'list'}); }
  const avatar = Array.from(document.querySelectorAll('#LeftColumn .Avatar[data-peer-id="' + __PEER__ + '"]'))
    .find((e) => e.closest('.ListItem-button') && hittable(e.closest('.ListItem-button')) && !/\\d{1,2}:\\d{2}|\\b\\w{3} \\d{1,2}\\b/.test(
      e.closest('.ListItem-button').innerText.split('\\n')[1] || ''));
  if (avatar) { avatar.closest('.ListItem-button').setAttribute('data-qa-target', 'peer'); return JSON.stringify({found: 'search'}); }
  return JSON.stringify({found: null});
})()""".replace("VIS", _VIS)

_TAG_START_JS = """(() => {
  VIS
  START_BUTTON
  document.querySelectorAll('[data-qa-target]').forEach((e) => e.removeAttribute('data-qa-target'));
  // Only this peer's chat: the page may have navigated since the state probe.
  const b = location.hash === '#' + __PEER__ ? startButton() : null;
  if (!b) return JSON.stringify({ok: false});
  b.setAttribute('data-qa-target', 'start');
  return JSON.stringify({ok: true});
})()""".replace("VIS", _VIS).replace("START_BUTTON", _START_BUTTON)

_SCROLL_BOTTOM_JS = """(() => {
  VIS
  document.querySelectorAll('[data-qa-target]').forEach((e) => e.removeAttribute('data-qa-target'));
  const b = Array.from(document.querySelectorAll('#MiddleColumn button')).find(
    (x) => vis(x) && /bottom/i.test(x.getAttribute('aria-label') || ''));
  if (!b) return JSON.stringify({tagged: false});
  b.setAttribute('data-qa-target', 'bottom');
  return JSON.stringify({tagged: true});
})()""".replace("VIS", _VIS)

# Snapshot of the rendered messages. MessageMeta (time/status) sits inside
# .text-content in Web A, so its text is cut from the end of the message text.
_MESSAGES_JS = """(() => {
  // Live innerText keeps line breaks; the time/status meta renders at its end.
  const clean = (el) => {
    if (!el) return '';
    let text = el.innerText || el.textContent || '';
    for (const meta of el.querySelectorAll('.MessageMeta, [data-ignore-on-paste]')) {
      const tail = (meta.innerText || meta.textContent || '').trim();
      text = text.trimEnd();
      if (tail && text.endsWith(tail)) text = text.slice(0, -tail.length);
    }
    return text.trim();
  };
  KEYBOARD
  const after = __AFTER__;
  const out = [];
  for (const m of document.querySelectorAll('#MiddleColumn .Message[data-message-id]')) {
    const id = Number(m.dataset.messageId);
    if (!Number.isInteger(id) || (after !== null && id <= after)) continue;
    // The status icon cross-fades: read the ACTIVE slide (the inactive one
    // still holds the previous state's icon).
    const st = m.querySelector('.MessageOutgoingStatus .Transition_slide-active i')
      || m.querySelector('.MessageOutgoingStatus i');
    const icon = st ? String(st.className) : '';
    const status = /icon-message-(\\w+)/.test(icon) ? icon.match(/icon-message-(\\w+)/)[1]
      : /icon-check/.test(icon) ? 'succeeded' : null;
    // A reply preview (.EmbeddedMessage) reuses .Audio/.File/.media-inner for the quoted
    // message, so only elements outside it describe this message.
    const own = (sel) => Array.from(m.querySelectorAll(sel)).find((e) => !e.closest('.EmbeddedMessage'));
    const audio = own('.Audio');
    // Current Web A renders photos/videos as .media-inner in a .message-content.media bubble.
    const kind = audio ? (/voice/i.test(audio.className) ? 'voice' : 'audio')
      : own('.File') ? 'document'
      : own('.Photo, .Video, .message-content.media .media-inner') ? 'media' : 'text';
    const reply = m.querySelector('.EmbeddedMessage');
    out.push({
      id, out: m.classList.contains('own'), text: clean(m.querySelector('.text-content')),
      kind, status,
      buttons: keyboard(m).map((b) => (b.textContent || '').trim()),
      reactions: Array.from(m.querySelectorAll('.Reactions .ReactionButton, .Reactions [class*="reaction"]'))
        .map((r) => (r.querySelector('img[alt]')?.getAttribute('alt') || r.textContent || '').trim()).filter(Boolean),
      reply_to_text: reply ? clean(reply) : null,
    });
  }
  return JSON.stringify(out);
})()""".replace("KEYBOARD", _KEYBOARD)

_CONTEXT_MENU_JS = """(() => {
  const m = document.querySelector('#MiddleColumn .Message[data-message-id="' + __ID__ + '"]');
  if (!m) return JSON.stringify({ok: false, reason: 'message-not-rendered'});
  m.scrollIntoView({block: 'center'});
  const c = m.querySelector('.message-content') || m;
  const r = c.getBoundingClientRect();
  c.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true,
    clientX: r.left + 10, clientY: r.top + 10, button: 2}));
  return JSON.stringify({ok: true});
})()"""

_TAG_REPLY_ITEM_JS = """(() => {
  VIS
  document.querySelectorAll('[data-qa-target]').forEach((e) => e.removeAttribute('data-qa-target'));
  const item = Array.from(document.querySelectorAll('.MenuItem')).find((e) => vis(e) && e.querySelector('.icon-reply'));
  if (!item) return JSON.stringify({ok: false});
  item.setAttribute('data-qa-target', 'reply');
  return JSON.stringify({ok: true});
})()""".replace("VIS", _VIS)

_REPLY_BAR_JS = """(() => {
  VIS
  const bar = Array.from(document.querySelectorAll('.Composer .EmbeddedMessage, .ComposerEmbeddedMessage .EmbeddedMessage')).find(vis);
  return JSON.stringify({active: !!bar, text: bar ? (bar.textContent || '').trim().slice(0, 200) : null});
})()""".replace("VIS", _VIS)

_TAG_BUTTON_JS = """(() => {
  VIS
  KEYBOARD
  document.querySelectorAll('[data-qa-target]').forEach((e) => e.removeAttribute('data-qa-target'));
  const norm = (s) => (s || '').normalize('NFKC').replace(/\\s+/g, ' ').trim();
  const m = document.querySelector('#MiddleColumn .Message[data-message-id="' + __ID__ + '"]');
  if (!m) return JSON.stringify({ok: false, reason: 'message-not-rendered', labels: []});
  const want = norm(__LABEL__);
  const buttons = keyboard(m);
  const labels = buttons.map((b) => norm(b.textContent));
  const exact = buttons.filter((b) => norm(b.textContent) === want);
  const hits = exact.length ? exact : buttons.filter((b) => norm(b.textContent).includes(want));
  if (hits.length !== 1) return JSON.stringify({ok: false, reason: hits.length ? 'ambiguous' : 'no-such-button', labels});
  m.scrollIntoView({block: 'center'});
  hits[0].setAttribute('data-qa-target', 'button');
  return JSON.stringify({ok: vis(hits[0]), reason: vis(hits[0]) ? null : 'button-hidden', label: norm(hits[0].textContent), labels});
})()""".replace("VIS", _VIS).replace("KEYBOARD", _KEYBOARD)

# Paste a File into the composer: Web A opens its "Send Audio/File/Photo"
# modal exactly as for a user's clipboard paste. Bytes arrive as base64 via
# stdin (eval --stdin), never argv.
_PASTE_FILE_JS = """(() => {
  const raw = atob(__B64__);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  const file = new File([bytes], __NAME__, {type: __MIME__});
  const dt = new DataTransfer();
  dt.items.add(file);
  const el = document.querySelector('#editable-message-text');
  if (!el) return JSON.stringify({ok: false, reason: 'no-composer'});
  el.focus();
  el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
  return JSON.stringify({ok: true});
})()"""

_TAG_MODAL_SEND_JS = """(() => {
  VIS
  document.querySelectorAll('[data-qa-target]').forEach((e) => e.removeAttribute('data-qa-target'));
  // Web A renamed the modal send glyph from icon-send to icon-new-send (2026-10).
  const send = Array.from(document.querySelectorAll('#portals button')).find((b) => vis(b) && b.querySelector('.icon-send, .icon-new-send'));
  if (!send) return JSON.stringify({ok: false});
  send.setAttribute('data-qa-target', 'modal-send');
  return JSON.stringify({ok: true});
})()""".replace("VIS", _VIS)


def _parse_message(raw) -> Optional[ChatMessage]:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), int):
        return None
    return ChatMessage(
        id=raw["id"],
        out=raw.get("out") is True,
        text=raw.get("text") if isinstance(raw.get("text"), str) else "",
        kind=raw.get("kind") if isinstance(raw.get("kind"), str) else "text",
        status=raw.get("status") if isinstance(raw.get("status"), str) else None,
        buttons=[b for b in raw.get("buttons") or [] if isinstance(b, str)],
        reactions=[r for r in raw.get("reactions") or [] if isinstance(r, str)],
        reply_to_text=raw.get("reply_to_text") if isinstance(raw.get("reply_to_text"), str) else None,
    )


class TelegramChat:
    """One peer's chat in Telegram Web A, driven through agent-browser."""

    def __init__(
        self,
        driver: ChatDriver,
        peer: int,
        username: Optional[str] = None,
        *,
        allow_non_bot: bool = False,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.driver = driver
        self.peer = int(peer)
        self.username = username.lstrip("@") if username else None
        self.allow_non_bot = allow_non_bot
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

    # -- state -------------------------------------------------------------

    def state(self) -> dict:
        data = self.driver.eval_json(_js(_STATE_JS, PEER=str(self.peer)))
        return data if isinstance(data, dict) else {}

    def _poll(self, probe: Callable[[], Optional[object]], timeout: float, what: str):
        deadline = self._clock() + timeout
        while True:
            result = probe()
            if result:
                return result
            if self._clock() >= deadline:
                raise ChatTimeoutError(f"timed out after {timeout:g}s waiting for {what}")
            self._sleep(_POLL_INTERVAL)

    def _ensure_web(self) -> dict:
        # A freshly launched session is still restoring/navigating; its first
        # eval can fail while the page context is being replaced.
        for attempt in range(3):
            try:
                state = self.state()
                break
            except BrowserError:
                if attempt == 2:
                    raise
                self._sleep(1.0)
        if not state.get("onWeb"):
            self.driver.open(TELEGRAM_WEB_URL)
        if not (state.get("onWeb") and (state.get("loggedIn") or state.get("authForm"))):
            # A restored session can already be on Web A with neither the chat
            # shell nor the auth form rendered yet.
            state = self._poll(
                lambda: (lambda s: s if s.get("loggedIn") or s.get("authForm") else None)(self.state()),
                _OPEN_TIMEOUT,
                "Telegram Web to load",
            )
        if state.get("authForm") or not state.get("loggedIn"):
            raise NotLoggedInError("Telegram Web is not logged in; run: qa telegram login")
        return state

    def open(self) -> dict:
        """Open the peer's chat (no reload when already open) at its latest message."""
        state = self._ensure_web()
        if not state.get("open"):
            # Room for long bot cards and readable proof screenshots.
            self.driver.batch([["set", "viewport", "1280", "1000"]])
            tag_peer = lambda: (self.driver.eval_json(_js(_TAG_PEER_JS, PEER=str(self.peer))) or {}).get("found")
            try:
                # A just-loaded shell renders its chat list a moment later.
                found = self._poll(tag_peer, 5.0, f"chat {self.peer} in the chat list")
            except ChatTimeoutError:
                found = None
            if not found and self.username:
                self.driver.batch([["click", _SEARCH_INPUT], ["keyboard", "inserttext", self.username]])
                found = self._poll(tag_peer, 10.0, f"search result for @{self.username}")
            if not found:
                raise ChatError(
                    f"chat {self.peer} is not in the visible chat list; pass --username to find it via search"
                )
            is_open = lambda: (lambda s: s if s.get("open") else None)(self.state())
            self.driver.batch([["click", '[data-qa-target="peer"]']])
            try:
                state = self._poll(is_open, 4.0, f"chat {self.peer} to open")
            except ChatTimeoutError:
                # With bot topics on, the first click only slides in the forum
                # panel; its "All Messages" entry is then the anchor that opens it.
                if not tag_peer():
                    raise
                self.driver.batch([["click", '[data-qa-target="peer"]']])
                state = self._poll(is_open, 10.0, f"chat {self.peer} to open")
        for _ in range(4):
            if not state.get("bottomButton"):
                break
            try:
                if (self.driver.eval_json(_SCROLL_BOTTOM_JS) or {}).get("tagged"):
                    self.driver.batch([["click", '[data-qa-target="bottom"]']])
            except BrowserError:
                pass  # best-effort: the button animates out while the list settles
            self._sleep(_POLL_INTERVAL)
            state = self.state()
        return state

    @staticmethod
    def _is_bot(state: dict) -> bool:
        return (state.get("subtitle") or "").strip().lower() in _BOT_SUBTITLES

    def _require_bot(self, state: dict) -> None:
        if not self.allow_non_bot and not self._is_bot(state):
            raise ChatGuardError(
                f"refusing to write: chat {self.peer} header does not identify a bot "
                f"(subtitle {state.get('subtitle')!r}); pass --allow-non-bot to override"
            )

    def _require_writable(self, state: Optional[dict] = None) -> dict:
        state = state if state is not None else self.open()
        self._require_bot(state)
        if not state.get("composer"):
            if state.get("startButton"):
                raise ChatGuardError(
                    f"the bot chat {self.peer} has not been started; send /start first"
                )
            raise ChatError("the message composer is not available in this chat")
        if state.get("draft"):
            raise ChatGuardError(
                "refusing to write: the composer holds a draft (the account's synced text); clear it first"
            )
        return state

    # -- reading -----------------------------------------------------------

    def messages(self, after: Optional[int] = None) -> List[ChatMessage]:
        raw = self.driver.eval_json(_js(_MESSAGES_JS, AFTER=after))
        if not isinstance(raw, list):
            raise ChatError("could not read the message list")
        return [m for m in (_parse_message(item) for item in raw) if m is not None]

    def last_id(self) -> int:
        return max((m.id for m in self.messages()), default=0)

    # Telegram Web A's bot-chat view (with topics on: "All Messages") can stop
    # rendering new messages; a wait re-opens the chat after this long without
    # a match, and again every time the same stretch passes.
    _STALE_SEC = 15.0

    def _refresh(self) -> None:
        """Leave and re-open the chat so Web A re-fetches its latest messages."""
        try:
            # Off the peer's hash `open` sees the chat as closed and re-clicks it.
            self.driver.open(TELEGRAM_WEB_URL + "#-1")
            self.open()
        except NotLoggedInError:
            raise  # a logged-out page never renders the message: fail fast (exit 3)
        except BrowserError:
            pass  # best effort: the next probe (and the next refresh) tells

    def wait_for(
        self,
        *,
        after: Optional[int] = None,
        message_id: Optional[int] = None,
        contains: Optional[str] = None,
        direction: str = "in",
        timeout: float = DEFAULT_WAIT_TIMEOUT,
    ) -> ChatMessage:
        """First message matching every given filter; newest-last order.

        direction: "in" (from the peer), "out" (from this account) or "any".
        message_id re-reads one message (e.g. an edited bot card).

        After ``_STALE_SEC`` without a match (and every ``_STALE_SEC`` after
        that) the chat is re-opened to force Web A to re-fetch its message
        list, so only waits longer than ``_STALE_SEC`` get a refresh."""

        def probe() -> Optional[ChatMessage]:
            for m in self.messages(after=None if message_id is not None else after):
                if message_id is not None and m.id != message_id:
                    continue
                if direction == "in" and m.out or direction == "out" and not m.out:
                    continue
                if contains is not None and not m.contains(contains):
                    continue
                return m
            return None

        what = " ".join(
            part
            for part in (
                f"message {message_id}" if message_id is not None else f"a message after {after}",
                f"containing {contains!r}" if contains else "",
                f"({direction})",
            )
            if part
        )
        deadline = self._clock() + timeout
        refresh_at = self._clock() + self._STALE_SEC
        while True:
            result = probe()
            if result:
                return result
            now = self._clock()
            if now >= deadline:
                raise ChatTimeoutError(f"timed out after {timeout:g}s waiting for {what}")
            if now >= refresh_at:
                self._refresh()
                refresh_at = self._clock() + self._STALE_SEC
            self._sleep(_POLL_INTERVAL)

    # -- writing -----------------------------------------------------------

    def _start_reply(self, message_id: int) -> None:
        opened = self.driver.eval_json(_js(_CONTEXT_MENU_JS, ID=str(message_id))) or {}
        if not opened.get("ok"):
            raise ChatError(f"cannot reply: message {message_id} is not rendered in the open chat")
        self._poll(
            lambda: (self.driver.eval_json(_TAG_REPLY_ITEM_JS) or {}).get("ok"), 5.0, "the Reply menu item"
        )
        self.driver.batch([["click", '[data-qa-target="reply"]']])
        self._poll(lambda: (self.driver.eval_json(_REPLY_BAR_JS) or {}).get("active"), 5.0, "the reply bar")

    def _confirm_own(self, before: int, match: Callable[[ChatMessage], bool], what: str) -> ChatMessage:
        def probe() -> Optional[ChatMessage]:
            for m in self.messages(after=before):
                if not m.out or not match(m):
                    continue
                if m.status == "failed":
                    raise ChatError(f"Telegram reports the {what} as failed to send")
                if m.status in ("succeeded", "read"):
                    return m
            return None

        return self._poll(probe, _CONFIRM_TIMEOUT, f"the {what} to be confirmed by Telegram")

    def _press_start(self) -> ChatMessage:
        """Click Web A's START button: for a bot it sends the same /start message."""
        before = self.last_id()
        if not (self.driver.eval_json(_js(_TAG_START_JS, PEER=str(self.peer))) or {}).get("ok"):
            raise ChatError("the START button disappeared before it could be clicked")
        self.driver.batch([["click", '[data-qa-target="start"]']])
        return self._confirm_own(before, lambda m: normalize(m.text) == "/start", "sent /start")

    def send_text(self, text: str, reply_to: Optional[int] = None) -> ChatMessage:
        if not text.strip() or "\n" in text:
            raise ChatGuardError("text must be one non-empty line")
        state = self.open()
        # START is for bots only, even under allow_non_bot: that override widens where
        # typing is allowed, never what a button click may start.
        if (text == "/start" and reply_to is None and not state.get("composer")
                and state.get("startButton") and self._is_bot(state)):
            return self._press_start()
        self._require_writable(state)
        before = self.last_id()
        if reply_to is not None:
            self._start_reply(reply_to)
        self.driver.batch([["click", _COMPOSER], ["keyboard", "inserttext", text], ["press", "Enter"]])
        want = normalize(text)
        return self._confirm_own(before, lambda m: normalize(m.text) == want, "sent message")

    def send_file(self, path: str, reply_to: Optional[int] = None) -> ChatMessage:
        self._require_writable()
        data = Path(path).read_bytes()
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise ChatGuardError(f"file must be 1..{MAX_UPLOAD_BYTES} bytes")
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        before = self.last_id()
        if reply_to is not None:
            self._start_reply(reply_to)
        pasted = self.driver.eval_json_stdin(
            _js(_PASTE_FILE_JS, B64=base64.b64encode(data).decode("ascii"), NAME=Path(path).name, MIME=mime)
        ) or {}
        if not pasted.get("ok"):
            raise ChatError("could not paste the file into the composer")
        self._poll(lambda: (self.driver.eval_json(_TAG_MODAL_SEND_JS) or {}).get("ok"), 10.0, "the send dialog")
        self.driver.batch([["click", '[data-qa-target="modal-send"]']])
        return self._confirm_own(before, lambda m: m.kind != "text", "sent file")

    def click_button(self, message_id: int, label: str) -> str:
        self._require_bot(self.open())
        tagged = self.driver.eval_json(_js(_TAG_BUTTON_JS, ID=str(message_id), LABEL=label)) or {}
        if not tagged.get("ok"):
            raise ChatError(
                f"cannot tap {label!r} on message {message_id}: {tagged.get('reason') or 'unknown'} "
                f"(buttons: {tagged.get('labels') or []})"
            )
        self.driver.batch([["click", '[data-qa-target="button"]']])
        return tagged.get("label") or label

    def screenshot(self, path: str) -> str:
        self.open()
        self.driver.screenshot(path)
        return path


def _emit(payload: dict, out) -> None:
    print(json.dumps(payload, ensure_ascii=False), file=out)


def run_chat_command(args, driver: ChatDriver, out=None, err=None) -> int:
    """Execute one `qa telegram chat <action>`; print one JSON object on stdout.

    Exit codes: 0 ok, 1 failure/timeout, 2 refused by a safety guard,
    3 not logged in."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    chat = TelegramChat(driver, args.peer, args.username, allow_non_bot=args.allow_non_bot)
    action = args.chat_command
    try:
        if action == "open":
            state = chat.open()
            _emit({"ok": True, "peer": chat.peer, "title": state.get("title"), "subtitle": state.get("subtitle"),
                   "last_id": chat.last_id()}, out)
        elif action == "read":
            chat.open()
            messages = chat.messages(after=args.after)
            if args.limit:
                messages = messages[-args.limit:]
            _emit({"ok": True, "messages": [m.to_json() for m in messages]}, out)
        elif action == "send":
            text = args.text if args.text is not None else Path(args.text_file).read_text(encoding="utf-8").strip()
            _emit({"ok": True, "message": chat.send_text(text, reply_to=args.reply_to).to_json()}, out)
        elif action == "send-file":
            _emit({"ok": True, "message": chat.send_file(args.file, reply_to=args.reply_to).to_json()}, out)
        elif action == "click":
            _emit({"ok": True, "message_id": args.message, "button": chat.click_button(args.message, args.button)}, out)
        elif action == "wait":
            chat.open()
            direction = {"peer": "in", "me": "out", "any": "any"}[args.sender]
            message = chat.wait_for(after=args.after, message_id=args.message, contains=args.contains,
                                    direction=direction, timeout=args.timeout)
            _emit({"ok": True, "message": message.to_json()}, out)
        elif action == "screenshot":
            _emit({"ok": True, "path": chat.screenshot(args.out)}, out)
        else:
            return 2
        return 0
    except BrowserMissingError:
        raise
    except NotLoggedInError as exc:
        code = 3
        message = str(exc)
    except ChatGuardError as exc:
        code = 2
        message = str(exc)
    except (ChatError, BrowserError, OSError, UnicodeDecodeError) as exc:
        code = 1
        message = str(exc)
    _emit({"ok": False, "error": message}, out)
    print(f"error: {message}", file=err)
    return code
