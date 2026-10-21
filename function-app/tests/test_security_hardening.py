"""Security review fixes: keys never leave via redirects, URLs, logs or deployment outputs."""

import http.server
import json
import logging
import re
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared import site24x7_client
from shared.site24x7_client import Site24x7Client, _json_log_parser, _send_logs_to_s247

ROOT = Path(__file__).resolve().parents[2]


class _Redirector(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        _Redirector.seen.append((self.path, dict(self.headers)))
        self.send_response(302)
        self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
        self.end_headers()

    do_POST = do_GET

    def log_message(self, *args):
        pass


@pytest.fixture
def redirector():
    _Redirector.seen = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def test_keyed_requests_do_not_follow_redirects(redirector):
    req = site24x7_client.urllib.request.Request(f"http://127.0.0.1:{redirector.server_port}/applog?deviceKey=secret")
    with pytest.raises(site24x7_client.urllib.error.HTTPError) as err:
        site24x7_client._KEYED_OPENER.open(req, timeout=5)
    assert err.value.code == 302
    assert [path for path, _ in _Redirector.seen] == ["/applog?deviceKey=secret"]  # never re-sent to /stolen


def test_api_calls_require_https(monkeypatch):
    monkeypatch.setenv("SITE24X7_API_KEY", "ab_" + "1" * 32)
    monkeypatch.setenv("SITE24X7_BASE_URL", "http://www.site24x7.com")
    client = Site24x7Client()
    with patch.object(site24x7_client._KEYED_OPENER, "open") as opened:
        assert client.get_supported_log_types() is None
        assert client.register_collector({"functionKey": "k"}) is False
    opened.assert_not_called()


def test_upload_answer_other_than_200_is_a_failure():
    response = MagicMock(status=202)
    response.getheaders.return_value = []
    with patch.object(site24x7_client._KEYED_OPENER, "open", return_value=response):
        with pytest.raises(RuntimeError):
            _send_logs_to_s247({"apiKey": "k", "logType": "t", "uploadDomain": "logc.site24x7.com"}, b"x", 1)


def test_unparseable_record_is_not_written_to_the_log(caplog):
    secret_record = {"password": "hunter2", "Time": object()}  # object() makes parsing fail
    config = {"jsonPath": [{"name": "Time", "key": "Time"}]}
    with caplog.at_level(logging.WARNING), \
            patch.object(site24x7_client, "_get_json_value", side_effect=RuntimeError("bad")):
        _json_log_parser([secret_record], config, None, None, None)
    assert "hunter2" not in caplog.text


def test_deployment_outputs_carry_no_key():
    template = (ROOT / "setup" / "azuredeploy.json").read_text()
    assert "listkeys" not in template.lower().split('"outputs"')[-1]
    for match in re.finditer(r'"outputs"\s*:\s*\{', template):
        outputs = template[match.end():match.end() + 3000]
        assert "listkeys" not in outputs.lower() and "?code=" not in outputs


def test_dashboard_keeps_the_key_out_of_urls_and_handlers_use_jsarg():
    import Dashboard
    html = Dashboard.DASHBOARD_HTML_TEMPLATE
    assert "code=${FUNC_KEY}" not in html and "?code=' + FUNC_KEY" not in html
    assert "history.replaceState" in html and "x-functions-key" in html
    # Inline handlers get values only through jsArg (a JS string literal, then HTML-escaped).
    # (A "[onclick=...]" CSS selector built from internal tab names is not a handler.)
    for handler in re.findall(r'(?<!\[)onclick="([^"]*\$\{[^"]*)"', html):
        for expr in re.findall(r"\$\{([^}]*)\}", handler):
            assert expr.startswith("jsArg(") or expr.startswith("!!"), handler


def test_saved_logtype_config_has_no_device_key():
    from shared import config_store
    with patch.object(config_store, "_write_blob", return_value=True) as write:
        assert config_store.save_logtype_config("AuditEvent", {"logType": "x", "apiKey": "secret-key"})
    assert "secret-key" not in write.call_args.args[1]
