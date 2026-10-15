"""A rejected Device Key must surface as an actionable error, never as 'no log types'."""

import io
import urllib.error
from unittest.mock import MagicMock, patch

from shared.site24x7_client import Site24x7Client

DEVICE_KEY = "ab_0123456789abcdef0123456789abcdef"


def _http_error(code, body):
    return urllib.error.HTTPError("https://plus.site24x7.com/x", code, "Bad Request", {}, io.BytesIO(body))


def test_invalid_device_key_is_explained_without_the_key(monkeypatch):
    monkeypatch.setenv("SITE24X7_API_KEY", DEVICE_KEY)
    monkeypatch.setenv("SITE24X7_BASE_URL", "https://www.site24x7.com")
    client = Site24x7Client()
    with patch("urllib.request.urlopen", side_effect=_http_error(400, b"ERROR : INVALID_DEVICE_KEY")):
        assert client.get_supported_log_types() is None
    assert client.last_error["http_status"] == 400
    assert "rejected the Device Key" in client.last_error["message"]
    assert "https://www.site24x7.com" in client.last_error["message"]
    assert DEVICE_KEY not in str(client.last_error)


def test_debug_event_carries_the_reason(monkeypatch):
    monkeypatch.setenv("SITE24X7_API_KEY", DEVICE_KEY)
    client = Site24x7Client()
    with patch("urllib.request.urlopen", side_effect=_http_error(400, b"ERROR : INVALID_DEVICE_KEY")), \
         patch("shared.debug_logger._append_json_array_rmw") as append:
        client.get_supported_log_types()
    event = append.call_args.args[1]
    assert "rejected the Device Key" in event["message"]
    assert event["details"]["http_status"] == 400
    assert DEVICE_KEY not in str(event)


def test_other_http_errors_keep_status_and_success_clears_error(monkeypatch):
    monkeypatch.setenv("SITE24X7_API_KEY", DEVICE_KEY)
    client = Site24x7Client()
    with patch("urllib.request.urlopen", side_effect=_http_error(503, b"busy")):
        client.get_supported_log_types()
    assert "HTTP 503" in client.last_error["message"]
    ok = MagicMock()
    ok.read.return_value = b'{"status":"SUCCESS","supported_types":[]}'
    with patch("urllib.request.urlopen", return_value=ok):
        client.get_supported_log_types()
    assert client.last_error is None


def test_connectivity_test_uses_api_host_and_reason(monkeypatch):
    # The dashboard test must call plus.* like a real scan, not the www.* base URL.
    monkeypatch.setenv("SITE24X7_API_KEY", DEVICE_KEY)
    monkeypatch.setenv("SITE24X7_BASE_URL", "https://www.site24x7.com")
    urls = []

    def fake_urlopen(req, timeout=None):
        urls.append(req.full_url)
        raise _http_error(400, b"ERROR : INVALID_DEVICE_KEY")

    from shared.debug_logger import test_s247_connectivity as run_connectivity
    with patch("urllib.request.urlopen", side_effect=fake_urlopen), \
         patch("socket.create_connection"), patch("shared.debug_logger._append_json_array_rmw"):
        result = run_connectivity()
    assert urls and all(u.startswith("https://plus.site24x7.com/applog/") for u in urls)
    assert "rejected the Device Key" in result["error"]
    assert DEVICE_KEY not in str(result)


def test_missing_device_key_has_actionable_error(monkeypatch):
    monkeypatch.delenv("SITE24X7_API_KEY", raising=False)
    client = Site24x7Client()
    assert client.get_supported_log_types() is None
    assert "not configured" in client.last_error["message"]


def test_http_error_echoing_json_key_never_leaks_configured_key(monkeypatch, caplog):
    monkeypatch.setenv("SITE24X7_API_KEY", DEVICE_KEY)
    client = Site24x7Client()
    body = ('{"deviceKey":"' + DEVICE_KEY + '","message":"bad request"}').encode()
    with patch("urllib.request.urlopen", side_effect=_http_error(500, body)), \
         patch("shared.debug_logger.log_event"):
        client.get_supported_log_types()
    assert DEVICE_KEY not in client.last_error["message"]
    assert DEVICE_KEY not in caplog.text


def test_connectivity_rejects_http_200_application_error(monkeypatch):
    monkeypatch.setenv("SITE24X7_API_KEY", DEVICE_KEY)
    from shared.debug_logger import test_s247_connectivity as run_connectivity
    response = MagicMock()
    response.read.return_value = b'{"status":"error","message":"rejected"}'
    with patch("urllib.request.urlopen", return_value=response), patch("socket.create_connection"):
        result = run_connectivity()
    assert result["logtype_supported"]["status"] == "error"
    assert result["logtype_check"]["status"] == "error"
    assert result["logtype_supported_ok"] is False


def test_logtype_lookup_without_status_counts_as_reachable(monkeypatch):
    # /applog/logtype returns the type definition with no `status` field (and an
    # account without the probed type answers with its own status): the API is
    # reachable, so the dashboard must not show "Logtype API: failed".
    monkeypatch.setenv("SITE24X7_API_KEY", DEVICE_KEY)
    from shared.debug_logger import test_s247_connectivity as run_connectivity
    supported = MagicMock()
    supported.read.return_value = b'{"status":"SUCCESS","supported_types":[{"logtype":"x"}]}'
    lookup = MagicMock()
    lookup.read.return_value = b'{"logType":"auditlogs","apiUpload":true,"dateField":"time"}'
    with patch("urllib.request.urlopen", side_effect=[supported, lookup]), patch("socket.create_connection"):
        result = run_connectivity()
    assert result["logtype_check"]["status"] == "ok"
    assert result["logtype_check"]["api_status"] is None
    assert "error" not in result
