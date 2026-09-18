import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa_cli.cli import main

def test_bot_probe_rejects_invalid_sha_and_non_https_before_network():
    assert main(["telegram", "bot-probe", "--base-url", "http://example.invalid", "--expected-sha", "a"*40]) == 2
    assert main(["telegram", "bot-probe", "--base-url", "https://example.invalid", "--expected-sha", "HEAD"]) == 2

def test_probe_separates_transport_from_authenticated_chat_qa(monkeypatch, capsys):
    from qa_cli import transport_probe
    calls=[]
    def request(url, payload=None):
        calls.append((url,payload))
        if url.endswith("healthz"):
            return 200, {"status":"alive","buildSha":"a"*40,"authenticatedWebhook":True}
        return 401, {}
    monkeypatch.setattr(transport_probe,"_request",request)
    assert main(["telegram","bot-probe","--base-url","https://example.invalid","--expected-sha","a"*40]) == 0
    result=json.loads(capsys.readouterr().out)
    assert result["passed"] is True
    assert result["evidence_scope"] == "http_transport_only"
    assert result["authenticated_chat_qa"] == "not_run"
    assert calls[1][1] == {"update_id":0}

def test_accepting_unsigned_webhook_fails_probe(monkeypatch, capsys):
    from qa_cli import transport_probe
    monkeypatch.setattr(transport_probe,"_request",lambda url,payload=None:(200,{"status":"alive","buildSha":"a"*40,"authenticatedWebhook":True}))
    assert main(["telegram","bot-probe","--base-url","https://example.invalid","--expected-sha","a"*40]) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False

def test_wrong_release_fails_probe(monkeypatch, capsys):
    from qa_cli import transport_probe
    monkeypatch.setattr(transport_probe,"_request",lambda url,payload=None:(401,{}) if payload else (200,{"status":"alive","buildSha":"b"*40,"authenticatedWebhook":True}))
    assert main(["telegram","bot-probe","--base-url","https://example.invalid","--expected-sha","a"*40]) == 1

def test_probe_does_not_expose_server_body(monkeypatch, capsys):
    from qa_cli import transport_probe
    monkeypatch.setattr(transport_probe,"_request",lambda url,payload=None:(500,{"secret":"PRIVATE"}))
    assert main(["telegram","bot-probe","--base-url","https://example.invalid","--expected-sha","a"*40]) == 1
    assert "PRIVATE" not in capsys.readouterr().out


def test_wall_timeout_is_enforced_by_subprocess(monkeypatch):
    import subprocess
    from qa_cli import transport_probe
    def stuck(argv, **kwargs):
        assert kwargs["timeout"] == transport_probe.REQUEST_WALL_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output="PRIVATE")
    monkeypatch.setattr(transport_probe.subprocess, "run", stuck)
    assert transport_probe._request("https://example.invalid/healthz") == (0, {})


def test_real_http_boundary_rejects_redirect_large_and_error_body():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from qa_cli import transport_probe
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/redirect":
                self.send_response(302); self.send_header("Location", "/private"); self.end_headers()
            elif self.path == "/large":
                self.send_response(200); self.end_headers(); self.wfile.write(b"x" * (transport_probe.MAX_RESPONSE_BYTES + 100))
            else:
                self.send_response(401); self.end_headers(); self.wfile.write(b'{"private":"DO_NOT_EXPORT"}')
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert transport_probe._request_direct(base + "/redirect") == (302, {})
        assert transport_probe._request_direct(base + "/large") == (200, {})
        assert transport_probe._request_direct(base + "/error") == (401, {})
        assert hits == ["/redirect", "/large", "/error"]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_request_worker_process_protocol():
    from qa_cli import transport_probe
    # A closed loopback port gives a bounded connection failure without external calls.
    assert transport_probe._request("http://127.0.0.1:1/healthz") == (0, {})
