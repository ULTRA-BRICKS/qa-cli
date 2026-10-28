"""Hermetic tests for `qa telegram chat ...` (qa_cli.chat).

Accessed via: `pytest tests/test_chat.py`. A FakePage stands in for the
agent-browser driver: it answers each page script by what the script asks
for (chat state, message list, element tagging) and records every batch, so
the tests observe exactly what would be typed or clicked. Never Chromium,
never web.telegram.org, never a real chat message.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys

import pytest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from qa_cli.browser import AgentBrowserDriver  # noqa: E402
from qa_cli.chat import CHAT_SESSION_NAME, _MESSAGES_JS, _STATE_JS, _TAG_MODAL_SEND_JS, _js, run_chat_command  # noqa: E402
from qa_cli.cli import build_parser, main  # noqa: E402

BOT = 8574086668


def msg(id_, text, out=False, status=None, buttons=(), kind="text"):
    return {"id": id_, "out": out, "text": text, "kind": kind, "status": status,
            "buttons": list(buttons), "reactions": [], "reply_to_text": None}


class FakePage:
    """Answers the chat module's page scripts from a small in-memory model."""

    def __init__(self, messages=(), subtitle="bot", draft=0, logged_in=True, on_send=None,
                 composer=True, start_button=False, on_start=None):
        self.messages = list(messages)
        self.subtitle = subtitle
        self.composer = composer
        self.start_button = start_button
        self.on_start = on_start  # called with (page) when START is clicked
        self.draft = draft
        self.logged_in = logged_in
        self.opened = True
        self.batches = []
        self.argv_evals = []
        self.on_send = on_send  # called with (page, typed_text) after Enter

    # driver protocol -------------------------------------------------------
    def open(self, url):
        self.opened_url = url

    def screenshot(self, path):
        self.shot = path

    def eval_json_stdin(self, js):
        return self.eval_json(js)

    def eval_json(self, js):
        self.argv_evals.append(js)
        if "onWeb" in js:
            return {"onWeb": True, "authForm": not self.logged_in, "loggedIn": self.logged_in,
                    "open": self.opened, "title": "Claude", "subtitle": self.subtitle,
                    "composer": self.composer, "startButton": self.start_button,
                    "draft": self.draft, "bottomButton": False}
        if "const after = " in js:
            after = re.search(r"const after = (null|\d+);", js).group(1)
            floor = -1 if after == "null" else int(after)
            return [m for m in self.messages if m["id"] > floor]
        if "'start'" in js:
            return {"ok": self.start_button}
        if "'button'" in js:
            return self._tag_button(js)
        return {"ok": True, "found": "list", "tagged": False, "active": True}

    def _tag_button(self, js):
        label = json.loads(re.search(r"const want = norm\((.*?)\);", js).group(1))
        target = int(json.loads(re.search(r'data-message-id="\' \+ (".*?") \+', js).group(1)))
        m = next((m for m in self.messages if m["id"] == target), None)
        if m is None:
            return {"ok": False, "reason": "message-not-rendered", "labels": []}
        hits = [b for b in m["buttons"] if b == label] or [b for b in m["buttons"] if label in b]
        if len(hits) != 1:
            return {"ok": False, "reason": "ambiguous" if hits else "no-such-button", "labels": m["buttons"]}
        self.tagged_button = hits[0]
        return {"ok": True, "label": hits[0], "labels": m["buttons"]}

    def batch(self, commands):
        self.batches.append(commands)
        if ["click", '[data-qa-target="start"]'] in commands and self.on_start:
            self.on_start(self)
        typed = [c[2] for c in commands if c[:2] == ["keyboard", "inserttext"]]
        if typed and ["press", "Enter"] in commands and self.on_send:
            self.on_send(self, typed[0])


def run(page, *argv):
    out = io.StringIO()
    args = build_parser().parse_args(["telegram", "chat", *argv])
    code = run_chat_command(args, page, out=out, err=io.StringIO())
    return code, json.loads(out.getvalue())


def typed_texts(page):
    return [c[2] for batch in page.batches for c in batch if c[:2] == ["keyboard", "inserttext"]]


def test_chat_session_is_isolated_from_login_session_and_never_saves_back():
    argv = AgentBrowserDriver(session=CHAT_SESSION_NAME, restore_save="never").build_argv("eval", "1")
    assert argv[1:7] == ["--session", "qa-telegram-chat", "--restore", "qa-telegram", "--restore-save", "never"]
    assert AgentBrowserDriver().build_argv("eval", "1")[1:5] == ["--session", "qa-telegram", "--restore", "qa-telegram"]


def test_send_types_via_stdin_batch_and_waits_until_telegram_confirms(monkeypatch):
    def deliver(page, text):
        page.messages.append(msg(12, text, out=True, status="pending"))

    page = FakePage([msg(10, "older")], on_send=deliver)
    # Telegram confirms the pending message while the CLI polls.
    monkeypatch.setattr("qa_cli.chat.time.sleep", lambda s: page.messages[-1].update(status="read"))
    code, payload = run(page, "send", "--peer", str(BOT), "--text", "/agent qa1 ping")
    assert code == 0, payload
    assert payload["message"]["id"] == 12 and payload["message"]["status"] == "read"
    assert typed_texts(page) == ["/agent qa1 ping"]
    assert not any("ping" in js for js in page.argv_evals), "typed text must not travel via argv"


def test_cli_entrypoint_routes_chat_commands_to_the_injected_driver(capsys):
    assert main(["telegram", "chat", "read", "--peer", str(BOT)], browser=FakePage([msg(5, "hi")])) == 0
    assert json.loads(capsys.readouterr().out)["messages"][0]["text"] == "hi"


def test_send_refuses_a_chat_that_is_not_a_bot():
    page = FakePage(subtitle="last seen recently")
    code, payload = run(page, "send", "--peer", str(BOT), "--text", "hello")
    assert code == 2 and "not identify a bot" in payload["error"]
    assert typed_texts(page) == []


def test_send_refuses_to_append_to_the_accounts_draft():
    page = FakePage(draft=5)
    code, payload = run(page, "send", "--peer", str(BOT), "--text", "hello")
    assert code == 2 and "draft" in payload["error"]
    assert typed_texts(page) == []


def test_send_reports_a_message_telegram_failed_to_deliver():
    page = FakePage(on_send=lambda p, t: p.messages.append(msg(3, t, out=True, status="failed")))
    code, payload = run(page, "send", "--peer", str(BOT), "--text", "hello")
    assert code == 1 and "failed to send" in payload["error"]


def test_not_logged_in_exits_3_with_the_login_command():
    code, payload = run(FakePage(logged_in=False), "read", "--peer", str(BOT))
    assert code == 3 and "qa telegram login" in payload["error"]


def test_wait_matches_styled_bot_text_after_nfkc_normalization():
    # The bot renders window names in Math Sans-Serif Bold; NFKC folds them to ASCII.
    styled = "✳️ [" + "".join(chr(0x1D5EE + ord(c) - ord("a")) for c in "qay") + "] ACK qa7x"
    page = FakePage([msg(20, "unrelated"), msg(21, styled)])
    code, payload = run(page, "wait", "--peer", str(BOT), "--after", "20", "--contains", "[qay] ACK qa7x")
    assert code == 0 and payload["message"]["id"] == 21


def test_wait_skips_own_messages_by_default_and_times_out(monkeypatch):
    page = FakePage([msg(30, "ACK qa7x", out=True)])
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr("qa_cli.chat.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("qa_cli.chat.time.sleep", lambda s: None)
    code, payload = run(page, "wait", "--peer", str(BOT), "--contains", "ACK qa7x", "--timeout", "10")
    assert code == 1 and "timed out" in payload["error"]


def test_wait_keeps_reopening_a_stale_chat_until_the_message_renders(monkeypatch):
    # Web A's stale view renders the new message only after the second re-open,
    # so a wait that refreshed just once would time out.
    page = FakePage([msg(50, "older")])
    leaves = []

    def leave_chat(url):
        leaves.append(url)
        if len(leaves) == 2:
            page.messages.append(msg(51, "QA-READY qa1"))

    page.open = leave_chat
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr("qa_cli.chat.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("qa_cli.chat.time.sleep", lambda s: None)
    code, payload = run(page, "wait", "--peer", str(BOT), "--after", "50", "--contains", "QA-READY qa1",
                        "--timeout", "120")
    assert code == 0 and payload["message"]["id"] == 51
    assert leaves == ["https://web.telegram.org/a/#-1"] * 2


def test_wait_fails_fast_with_exit_3_when_the_session_logs_out_mid_wait(monkeypatch):
    page = FakePage([msg(50, "older")])

    def leave_chat(url):
        page.logged_in = False

    page.open = leave_chat
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr("qa_cli.chat.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("qa_cli.chat.time.sleep", lambda s: None)
    code, payload = run(page, "wait", "--peer", str(BOT), "--after", "50", "--timeout", "600")
    assert code == 3 and "qa telegram login" in payload["error"]


def test_click_taps_exactly_one_matching_button():
    page = FakePage([msg(40, "Question", buttons=["Alpha", "Beta"])])
    code, payload = run(page, "click", "--peer", str(BOT), "--message", "40", "--button", "Beta")
    assert code == 0 and payload["button"] == "Beta"
    assert page.batches[-1] == [["click", '[data-qa-target="button"]']]


def test_click_refuses_an_ambiguous_label_and_lists_the_buttons():
    page = FakePage([msg(41, "Pick", buttons=["Alpha one", "Alpha two"])])
    code, payload = run(page, "click", "--peer", str(BOT), "--message", "41", "--button", "Alpha")
    assert code == 1 and "ambiguous" in payload["error"] and "Alpha two" in payload["error"]
    assert all(batch != [["click", '[data-qa-target="button"]']] for batch in page.batches)


def test_click_refuses_a_chat_that_is_not_a_bot():
    page = FakePage([msg(40, "Question", buttons=["Alpha"])], subtitle="last seen recently")
    code, payload = run(page, "click", "--peer", str(BOT), "--message", "40", "--button", "Alpha")
    assert code == 2 and "--allow-non-bot" in payload["error"]
    assert all(batch != [["click", '[data-qa-target="button"]']] for batch in page.batches)


def test_placeholder_text_inside_a_value_is_not_substituted_again():
    js = _js("f(__NAME__, __MIME__)", NAME="note__MIME__.ogg", MIME="audio/ogg")
    assert js == 'f("note__MIME__.ogg", "audio/ogg")'


def test_read_after_returns_only_newer_messages():
    page = FakePage([msg(1, "a"), msg(2, "b"), msg(3, "c")])
    code, payload = run(page, "read", "--peer", str(BOT), "--after", "1", "--limit", "1")
    assert code == 0 and [m["id"] for m in payload["messages"]] == [3]


# Minimal DOM for _TAG_MODAL_SEND_JS: #portals buttons, each with icon classes; hidden
# buttons have no client rects. querySelector supports the comma list the script uses.
_MODAL_SEND_HARNESS = """
const spec = JSON.parse(require('fs').readFileSync(0, 'utf8'));
globalThis.getComputedStyle = () => ({display: 'block', visibility: 'visible', opacity: '1'});
const buttons = spec.buttons.map((b) => ({
  nodeType: 1, parentElement: null, attrs: {}, label: b.label,
  getClientRects: () => (b.visible ? [{}] : []),
  querySelector(sel) {
    return sel.split(',').map((s) => s.trim().replace(/^\\./, '')).some((c) => b.icons.includes(c)) ? {} : null;
  },
  setAttribute(k, v) { this.attrs[k] = v; },
  removeAttribute(k) { delete this.attrs[k]; },
}));
globalThis.document = {
  querySelectorAll(sel) {
    if (sel === '#portals button') return buttons;
    if (sel === '[data-qa-target]') return buttons.filter((b) => 'data-qa-target' in b.attrs);
    return [];
  },
};
const result = JSON.parse(eval(spec.script));
const tagged = buttons.filter((b) => b.attrs['data-qa-target'] === 'modal-send').map((b) => b.label);
process.stdout.write(JSON.stringify({ok: result.ok, tagged}));
"""


def _tag_modal_send(buttons):
    if not shutil.which("node"):
        pytest.skip("node is required to run page scripts")
    proc = subprocess.run(
        ["node", "-e", _MODAL_SEND_HARNESS],
        input=json.dumps({"script": _TAG_MODAL_SEND_JS, "buttons": buttons}),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_modal_send_tags_web_a_new_send_icon():
    result = _tag_modal_send([
        {"label": "cancel", "icons": ["icon", "icon-close"], "visible": True},
        {"label": "emoji", "icons": ["icon", "icon-smile"], "visible": True},
        {"label": "send", "icons": ["icon", "icon-new-send"], "visible": True},
    ])
    assert result == {"ok": True, "tagged": ["send"]}


def test_modal_send_still_tags_the_legacy_send_icon_and_skips_hidden_ones():
    result = _tag_modal_send([
        {"label": "hidden-send", "icons": ["icon", "icon-new-send"], "visible": False},
        {"label": "send", "icons": ["icon", "icon-send"], "visible": True},
    ])
    assert result == {"ok": True, "tagged": ["send"]}


# A tiny DOM for _MESSAGES_JS: nodes are {tag?, id?, cls?, attrs?, text?, children?};
# selectors support comma lists, descendant combinators and compound tag/#id/.class/[attr]
# parts — the subset the page script uses.
_MESSAGES_HARNESS = """
const spec = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const parseCompound = (s) => ({
  tag: (s.match(/^[a-zA-Z]+/) || [null])[0],
  id: (s.match(/#([\\w-]+)/) || [null, null])[1],
  classes: [...s.matchAll(/\\.([\\w-]+)/g)].map((x) => x[1]),
  attrs: [...s.matchAll(/\\[([\\w-]+)(?:([\\^$]?)="([^"]*)")?\\]/g)].map((x) => ({name: x[1], op: x[2] || '', value: x[3]})),
});
const matchesCompound = (el, c) => (!c.tag || el.tagName === c.tag.toUpperCase())
  && (!c.id || el.id === c.id) && c.classes.every((k) => el.classList.contains(k))
  && c.attrs.every((a) => {
    const v = a.name === 'id' ? el.id : el.attrs[a.name];
    if (v === undefined) return false;
    return a.op === '^' ? v.startsWith(a.value) : a.op === '$' ? v.endsWith(a.value) : true;
  });
const matchesComplex = (el, parts) => {
  if (!matchesCompound(el, parts[parts.length - 1])) return false;
  let rest = parts.slice(0, -1);
  for (let p = el.parentElement; p && rest.length; p = p.parentElement) {
    if (matchesCompound(p, rest[rest.length - 1])) rest = rest.slice(0, -1);
  }
  return rest.length === 0;
};
const matches = (el, sel) => sel.split(',').some((s) => matchesComplex(el, s.trim().split(/\\s+/).map(parseCompound)));
function build(node, parent) {
  const cls = (node.cls || '').split(/\\s+/).filter(Boolean);
  const el = {
    tagName: (node.tag || 'div').toUpperCase(), id: node.id || '', attrs: node.attrs || {},
    className: cls.join(' '), parentElement: parent, children: [],
    classList: {contains: (k) => cls.includes(k)},
    get dataset() { return {messageId: this.attrs['data-message-id']}; },
    get textContent() { return (node.text || '') + this.children.map((c) => c.textContent).join(''); },
    get innerText() { return this.textContent; },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    closest(sel) { for (let n = this; n; n = n.parentElement) if (matches(n, sel)) return n; return null; },
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => n.children.forEach((c) => { if (matches(c, sel)) out.push(c); walk(c); });
      walk(this);
      return out;
    },
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
    getClientRects() { return [{}]; },
  };
  el.children = (node.children || []).map((c) => build(c, el));
  return el;
}
const root = build(spec.tree, null);
globalThis.getComputedStyle = () => ({display: 'block', visibility: 'visible', opacity: '1'});
globalThis.location = spec.location || {origin: '', pathname: '', hash: ''};
globalThis.document = {
  querySelectorAll: (sel) => root.querySelectorAll(sel),
  querySelector: (sel) => root.querySelector(sel),
};
process.stdout.write(eval(spec.script));
"""


def _message(id_, *content, bubble="", outside=()):
    """One rendered Web A message: its bubble (.message-content) plus elements outside it."""
    return {"cls": "Message own", "attrs": {"data-message-id": str(id_)}, "children": [
        {"cls": "message-content-wrapper", "children": [
            {"cls": f"message-content {bubble}", "children": list(content)},
            *outside,
        ]},
    ]}


def _preview(*children):
    return {"cls": "EmbeddedMessage", "children": list(children)}


def _render_messages(*messages):
    if not shutil.which("node"):
        pytest.skip("node is required to run page scripts")
    tree = {"children": [{"id": "MiddleColumn", "children": list(messages)}]}
    proc = subprocess.run(
        ["node", "-e", _MESSAGES_HARNESS],
        input=json.dumps({"script": _js(_MESSAGES_JS, AFTER=None), "tree": tree}),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return {m["id"]: m for m in json.loads(proc.stdout)}


def test_messages_classify_current_web_a_photos_and_ignore_reply_previews():
    rendered = _render_messages(
        _message(1, {"cls": "media-inner", "children": [{"tag": "img", "cls": "full-media"}]}, bubble="media"),
        _message(2, _preview({"cls": "message-content media", "children": [{"cls": "media-inner"}]}),
                 {"cls": "text-content", "text": "nice photo"}),
        _message(3, _preview({"cls": "Audio voice"}), {"cls": "text-content", "text": "re voice"}),
        _message(4, _preview({"cls": "File"}), {"cls": "text-content", "text": "re file"}),
        _message(5, {"cls": "Audio voice"}),
    )
    assert {i: m["kind"] for i, m in rendered.items()} == {
        1: "media", 2: "text", 3: "text", 4: "text", 5: "voice",
    }


def test_messages_report_inline_keys_but_not_reactions_outside_the_bubble():
    rendered = _render_messages(
        _message(1, {"cls": "text-content", "text": "card"}, outside=(
            {"cls": "InlineButtons", "children": [{"tag": "button", "cls": "Button", "text": "Yes"}]},
            {"cls": "Reactions is-outside", "children": [
                {"tag": "button", "cls": "Button message-reaction", "text": "E"},
            ]},
        )),
    )
    assert rendered[1]["buttons"] == ["Yes"]


# -- header: title/subtitle come from the header's own elements, not avatar initials ----------

def _header(*, avatar_letters="HS", name="HyperCalendar Stage", status="bot"):
    """Web A's .MiddleHeader for an initials avatar (no photo): the avatar's letters come
    first in the header's text, then the chat name, then the status line."""
    return {"cls": "MiddleHeader", "children": [{"cls": "ChatInfo", "children": [
        {"cls": "Avatar no-photo", "children": [
            {"cls": "inner", "children": [{"tag": "span", "cls": "letters", "text": avatar_letters}]},
        ]},
        {"cls": "info", "children": [
            {"cls": "title", "children": [{"tag": "h3", "cls": "fullName", "text": name}]},
            *([{"tag": "span", "cls": "status", "children": [
                {"tag": "span", "cls": "user-status", "text": status}]}] if status else []),
        ]},
    ]}]}


def _footer(*, composer=False, start=False):
    kids = []
    if composer:
        kids.append({"id": "editable-message-text", "text": ""})
    if start:
        kids.append({"tag": "button", "cls": "Button primary fluid", "text": "START"})
    return {"cls": "middle-column-footer", "children": kids}


def _read_state(header, footer, peer=BOT):
    if not shutil.which("node"):
        pytest.skip("node is required to run page scripts")
    tree = {"children": [
        {"id": "Main", "children": [
            {"id": "MiddleColumn", "children": [header, footer]},
        ]},
    ]}
    location = {"origin": "https://web.telegram.org", "pathname": "/a/", "hash": f"#{peer}"}
    proc = subprocess.run(
        ["node", "-e", _MESSAGES_HARNESS],
        input=json.dumps({"script": _js(_STATE_JS, PEER=str(peer)), "tree": tree, "location": location}),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_state_reads_title_and_subtitle_from_header_elements_not_avatar_initials():
    state = _read_state(_header(), _footer(composer=True))
    assert state["open"] is True
    assert (state["title"], state["subtitle"]) == ("HyperCalendar Stage", "bot")


def test_state_keeps_a_non_bot_subtitle_for_an_initials_avatar_header():
    state = _read_state(_header(status="last seen recently"), _footer(composer=True))
    assert (state["title"], state["subtitle"]) == ("HyperCalendar Stage", "last seen recently")


def test_state_reports_start_button_when_the_composer_is_replaced_by_it():
    state = _read_state(_header(), _footer(start=True))
    assert state["composer"] is False and state["startButton"] is True
    state = _read_state(_header(), _footer(composer=True))
    assert state["composer"] is True and state["startButton"] is False


# -- never-started bot chat: START button instead of the composer ------------------------------

START_CLICK = ["click", '[data-qa-target="start"]']


def _starting_page(**kwargs):
    def press_start(page):
        page.start_button = False
        page.composer = True
        page.messages.append(msg(7, "/start", out=True, status="read"))

    return FakePage([msg(3, "older")], composer=False, start_button=True, on_start=press_start, **kwargs)


def _clicked_start(page):
    return any(START_CLICK in batch for batch in page.batches)


def test_send_start_clicks_the_start_button_in_a_bot_chat_and_returns_the_confirmed_message():
    page = _starting_page()
    code, payload = run(page, "send", "--peer", str(BOT), "--text", "/start")
    assert code == 0, payload
    assert payload["message"]["id"] == 7 and payload["message"]["text"] == "/start"
    assert _clicked_start(page)
    assert typed_texts(page) == []


def test_send_other_text_in_an_unstarted_bot_chat_is_refused_without_clicking():
    page = _starting_page()
    code, payload = run(page, "send", "--peer", str(BOT), "--text", "hello")
    assert code == 2 and "has not been started" in payload["error"] and "/start" in payload["error"]
    assert not _clicked_start(page)
    assert typed_texts(page) == []


def test_send_start_with_arguments_is_not_the_start_button():
    page = _starting_page()
    code, payload = run(page, "send", "--peer", str(BOT), "--text", "/start payload")
    assert code == 2 and "has not been started" in payload["error"]
    assert not _clicked_start(page)


def test_start_is_never_clicked_in_a_non_bot_chat():
    page = _starting_page(subtitle="last seen recently")
    code, payload = run(page, "send", "--peer", str(BOT), "--text", "/start")
    assert code == 2 and "not identify a bot" in payload["error"]
    assert not _clicked_start(page)


def test_send_start_reports_a_start_that_telegram_failed_to_deliver():
    def fail(page):
        page.messages.append(msg(7, "/start", out=True, status="failed"))

    page = FakePage(composer=False, start_button=True, on_start=fail)
    code, payload = run(page, "send", "--peer", str(BOT), "--text", "/start")
    assert code == 1 and "failed" in payload["error"]


def test_start_is_never_clicked_in_a_non_bot_chat_even_with_allow_non_bot():
    page = _starting_page(subtitle="last seen recently")
    code, payload = run(page, "send", "--peer", str(BOT), "--text", "/start", "--allow-non-bot")
    assert code == 2 and "has not been started" in payload["error"]
    assert not _clicked_start(page)


# -- username search recovery -------------------------------------------------


class SearchPage(FakePage):
    """Web A search model: inserttext/fill set the box value; the peer's search
    result shows only while the value equals `result_for`. `wipe` makes the page
    clear the box at the first poll sleep after typing (Web A clearing a cold query)."""

    def __init__(self, result_for="somebot", clear_first_typing=False, in_list=False, has_input=True, instant=False):
        super().__init__([msg(1, "hi")], subtitle="bot")
        self.result_for = result_for
        self.value = ""
        self.clear_first_typing = clear_first_typing
        self.in_list = in_list
        self.instant = instant
        self.has_input = has_input
        self.opened = False
        self.metadata_evals = 0
        self.wipe = False
        self.settled = True  # search results render one poll after typing

    def eval_json(self, js):
        if "onWeb" not in js and "telegram-search-input" in js:
            self.metadata_evals += 1
            return {"present": self.has_input, "length": len(self.value)}
        if "found: 'list'" in js:
            if self.in_list:
                return {"found": "list"}
            return {"found": "search" if self.settled and self.value == self.result_for else None}
        return super().eval_json(js)

    def batch(self, commands):
        self.batches.append(commands)
        for c in commands:
            if c[:2] == ["keyboard", "inserttext"]:
                self.value = c[2]
                self.settled = self.instant
                if self.clear_first_typing:
                    self.clear_first_typing = False
                    self.wipe = True
            elif c[0] == "fill":
                self.value = c[2]
                self.settled = self.instant
            elif c == ["click", '[data-qa-target="peer"]']:
                self.opened = True


def open_search(page):
    from qa_cli.chat import ChatTimeoutError, TelegramChat

    now = [0.0]

    def sleep(seconds):
        now[0] += seconds
        if page.wipe:
            page.wipe = False
            page.value = ""
        page.settled = True

    chat = TelegramChat(page, BOT, "somebot", clock=lambda: now[0], sleep=sleep)
    try:
        return chat.open(), now[0], None
    except ChatTimeoutError as error:
        return None, now[0], error


def fill_batches(page):
    return [b for b in page.batches if any(c[0] == "fill" for c in b)]


def test_search_query_cleared_by_the_page_is_re_entered_once():
    page = SearchPage(clear_first_typing=True)
    state, _, error = open_search(page)
    assert error is None and state["open"]
    assert fill_batches(page) == [[["click", "#telegram-search-input"], ["fill", "#telegram-search-input", "somebot"]]]


def test_search_with_a_stable_query_and_no_result_times_out_without_recovery():
    page = SearchPage(result_for="nobody")
    _, elapsed, error = open_search(page)
    assert error is not None and "search result for @somebot" in str(error)
    assert fill_batches(page) == []
    assert 15.0 <= elapsed < 15.0 + 1.4


def test_search_recovers_at_most_once_and_keeps_the_original_deadline():
    class Stubborn(SearchPage):
        def batch(self, commands):
            super().batch(commands)
            if any(c[0] in ("fill", "keyboard") for c in commands):
                self.wipe = True

    page = Stubborn()
    _, elapsed, error = open_search(page)
    assert error is not None and "timed out after 10s" in str(error)
    assert len(fill_batches(page)) == 1
    assert 15.0 <= elapsed < 15.0 + 1.4


def test_peer_found_in_the_chat_list_never_reads_the_search_input():
    page = SearchPage(in_list=True)
    state, _, error = open_search(page)
    assert error is None and state["open"]
    assert page.metadata_evals == 0 and fill_batches(page) == [] and typed_texts(page) == []


def test_peer_found_on_the_first_search_poll_needs_no_input_read():
    page = SearchPage(instant=True)
    state, _, error = open_search(page)
    assert error is None and state["open"]
    assert page.metadata_evals == 0 and fill_batches(page) == []


def test_unexpected_non_empty_search_input_is_left_alone():
    class Typed(SearchPage):
        def batch(self, commands):
            super().batch(commands)
            if any(c[:2] == ["keyboard", "inserttext"] for c in commands):
                self.value = "something else"

    page = Typed()
    _, _, error = open_search(page)
    assert error is not None and "search result for @somebot" in str(error)
    assert fill_batches(page) == [] and page.value == "something else"


def test_missing_search_input_is_left_alone():
    page = SearchPage(result_for="nobody", has_input=False)
    _, _, error = open_search(page)
    assert error is not None and fill_batches(page) == []
