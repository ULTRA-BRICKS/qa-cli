"""Bounded token-free deployment proof, explicitly not authenticated chat QA."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import subprocess
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE_BYTES = 16384
TIMEOUT_SECONDS = 8
REQUEST_WALL_TIMEOUT_SECONDS = 10
SHA_RE = re.compile(r"[0-9a-f]{40}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_direct(url: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                return response.status, {}
            try:
                parsed = json.loads(raw)
            except (ValueError, UnicodeError):
                parsed = {}
            return response.status, parsed if isinstance(parsed, dict) else {}
    except urllib.error.HTTPError as error:
        # No error body, URL or response text is copied into QA artifacts.
        error.close()
        return error.code, {}
    except (OSError, ValueError):
        return 0, {}


def _request(url: str, payload=None):
    # A subprocess boundary enforces wall time even for byte-trickling peers.
    # No shell, credentials, message content, stderr or response bodies escape it.
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--request", url, json.dumps(payload)],
            capture_output=True, text=True, timeout=REQUEST_WALL_TIMEOUT_SECONDS, check=False,
        )
        if result.returncode != 0:
            return 0, {}
        status, body = json.loads(result.stdout)
        return status, body if isinstance(body, dict) else {}
    except (subprocess.TimeoutExpired, OSError, ValueError, TypeError):
        return 0, {}


def run_bot_probe(base_url: str, expected_sha: str) -> int:
    try:
        url = urllib.parse.urlsplit(base_url)
        valid = (url.scheme == "https" and bool(url.hostname) and not url.username
                 and not url.password and not url.query and not url.fragment
                 and url.path in ("", "/") and url.port in (None, 443))
    except ValueError:
        valid = False
    if not valid or not SHA_RE.fullmatch(expected_sha):
        print("bot-probe requires an HTTPS origin and an exact 40-character commit SHA", file=sys.stderr)
        return 2
    origin = base_url.rstrip("/")
    started = time.monotonic()
    status, health = _request(origin + "/healthz")
    unauthorized_status, _ = _request(origin + "/webhook", {"update_id": 0})
    observed_sha = health.get("buildSha")
    if not isinstance(observed_sha, str) or not SHA_RE.fullmatch(observed_sha):
        observed_sha = None
    passed = (status == 200 and health.get("status") == "alive"
              and observed_sha == expected_sha and health.get("authenticatedWebhook") is True
              and unauthorized_status in (401, 403))
    result = {
        "evidence_scope": "http_transport_only",
        "authenticated_chat_qa": "not_run",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "expected_sha": expected_sha,
        "observed_sha": observed_sha,
        "health_status": status,
        "unauthenticated_webhook_status": unauthorized_status,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "passed": passed,
    }
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--request":
        raise SystemExit(2)
    print(json.dumps(_request_direct(sys.argv[2], json.loads(sys.argv[3]))))
