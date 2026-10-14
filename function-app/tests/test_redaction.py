"""Debug logs and downloads must be safe to send to Site24x7 support."""

from unittest.mock import patch

from GetDebugInfo import _as_log_file
from shared.redaction import MASK, redact

SECRETS = ["devkey-123456", "fnkey-abcdef", "c2lnbmF0dXJl", "YWNjb3VudGtleQ==", "eyJhbGciOi.payload"]


def test_credentials_are_masked_but_diagnostics_survive():
    event = {
        "message": "GET https://plus.site24x7.com/applog/logtype?deviceKey=devkey-123456&logType=gw failed",
        "url": "https://acct.blob.core.windows.net/c/b.json?sv=2022-11-02&sig=c2lnbmF0dXJl&se=2026-10-15",
        "conn": "DefaultEndpointsProtocol=https;AccountName=s247;AccountKey=YWNjb3VudGtleQ==;EndpointSuffix=core.windows.net",
        "header": "Authorization: Bearer eyJhbGciOi.payload",
        "dashboard": "https://app.azurewebsites.net/api/dashboard?code=fnkey-abcdef",
        "details": {"functionKey": "fnkey-abcdef", "cp_key": "acct/c/b.json",
                    "error": "error code=500", "SITE24X7_API_KEY": "***set***"},
    }
    out = redact(event)
    text = str(out)
    for secret in SECRETS:
        assert secret not in text
    assert out["details"]["functionKey"] == MASK
    assert out["details"]["cp_key"] == "acct/c/b.json"
    assert out["details"]["error"] == "error code=500"
    assert out["details"]["SITE24X7_API_KEY"] == "***set***"
    assert "logType=gw" in out["message"] and "AccountName=s247" in out["conn"]


def test_log_event_stores_redacted_copy():
    with patch("shared.debug_logger._append_json_array_rmw") as append:
        from shared.debug_logger import log_event
        log_event("error", "Test", "upload failed ?code=fnkey-abcdef", {"sas": "c2lnbmF0dXJl"})
    stored = append.call_args.args[1]
    assert "fnkey-abcdef" not in str(stored) and "c2lnbmF0dXJl" not in str(stored)


def test_log_file_is_readable_and_oldest_first():
    info = redact({"generated_at": "t", "function_app": "s247-diag-func-oct14", "region": "West Europe",
                   "error_count": 1, "warning_count": 0, "config_issues": [],
                   "recent_events": [
                       {"timestamp": "2026-10-14T12:00", "level": "error", "component": "B",
                        "message": "newer ?code=fnkey-abcdef"},
                       {"timestamp": "2026-10-14T11:00", "level": "info", "component": "A",
                        "message": "older"}],
                   "environment": {"SITE24X7_API_KEY": "***set***"}})
    text = _as_log_file(info)
    assert text.index("older") < text.index("newer")
    assert "ERROR   [B] newer ?code=***" in text
    assert "fnkey-abcdef" not in text
    assert "## environment" in text
