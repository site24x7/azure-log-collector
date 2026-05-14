"""Tests for GetDebugInfo function."""

import json
import sys
import types
import unittest.mock as mock
import pytest


# ─── Mock azure.functions if not installed ────────────────────────────────

if "azure.functions" not in sys.modules:
    _af = types.ModuleType("azure.functions")

    class _HttpRequest:
        def __init__(self, method="GET", url="/api/debug", params=None, body=b""):
            self.method = method
            self.url = url
            self.params = params or {}
            self.body = body

    class _HttpResponse:
        def __init__(self, body=None, status_code=200, mimetype="text/plain", headers=None):
            self._body = body.encode("utf-8") if isinstance(body, str) else (body or b"")
            self.status_code = status_code
            self.mimetype = mimetype
            self.headers = dict(headers or {})
        def get_body(self):
            return self._body

    _af.HttpRequest = _HttpRequest
    _af.HttpResponse = _HttpResponse
    # Only register azure.functions; don't clobber the real 'azure' package
    sys.modules["azure.functions"] = _af
    import azure
    azure.functions = _af

import azure.functions as func


# ─── Helpers ──────────────────────────────────────────────────────────────

def _make_req(params=None):
    """Create a mock HTTP request with query params."""
    return func.HttpRequest(
        method="GET",
        url="/api/debug",
        params=params or {},
        body=b"",
    )


# Patch targets are inside the function body (lazy imports), so we must
# patch at the source module level.
_DL = "shared.debug_logger"
_CS = "shared.config_store"


# ─── Tests ────────────────────────────────────────────────────────────────

class TestGetDebugInfo:
    """GET /api/debug endpoint tests."""

    @mock.patch(f"{_CS}.get_configured_resources", return_value={})
    @mock.patch(f"{_CS}.get_disabled_log_types", return_value=[])
    @mock.patch(f"{_CS}.get_all_logtype_configs", return_value={"S247_AADNonInteractiveUserSignInLogs": {}})
    @mock.patch(f"{_CS}.get_scan_state", return_value={"last_scan": "2025-01-01T00:00:00Z"})
    @mock.patch(f"{_DL}.get_processing_stats", return_value=[])
    @mock.patch(f"{_DL}.get_recent_events", return_value=[])
    @mock.patch(f"{_DL}.validate_config", return_value=[])
    def test_basic_debug_info(self, mock_vc, mock_re, mock_ps, mock_ss,
                               mock_lc, mock_dl, mock_cr):
        from GetDebugInfo import main
        resp = main(_make_req())
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert "generated_at" in data
        assert data["config_issues"] == []
        assert data["recent_events"] == []
        assert data["scan_state"]["last_scan"] == "2025-01-01T00:00:00Z"
        assert data["logtype_summary"]["configured_count"] == 1
        assert "s247_connectivity" not in data  # not requested

    @mock.patch(f"{_CS}.get_configured_resources", return_value={})
    @mock.patch(f"{_CS}.get_disabled_log_types", return_value=["S247_X"])
    @mock.patch(f"{_CS}.get_all_logtype_configs", return_value={})
    @mock.patch(f"{_CS}.get_scan_state", return_value={})
    @mock.patch(f"{_DL}.get_processing_stats", return_value=[])
    @mock.patch(f"{_DL}.get_recent_events", return_value=[
        {"level": "error", "message": "fail", "timestamp": "2025-01-01T00:00:00Z"},
        {"level": "warning", "message": "warn", "timestamp": "2025-01-01T00:01:00Z"},
    ])
    @mock.patch(f"{_DL}.validate_config", return_value=[
        {"severity": "error", "message": "SITE24X7_API_KEY not set"}
    ])
    def test_counts_errors_and_warnings(self, mock_vc, mock_re, mock_ps,
                                         mock_ss, mock_lc, mock_dl, mock_cr):
        from GetDebugInfo import main
        resp = main(_make_req())
        data = json.loads(resp.get_body())
        assert data["error_count"] == 1
        assert data["warning_count"] == 1
        assert len(data["config_issues"]) == 1
        assert data["logtype_summary"]["disabled_count"] == 1

    @mock.patch(f"{_CS}.get_configured_resources", return_value={})
    @mock.patch(f"{_CS}.get_disabled_log_types", return_value=[])
    @mock.patch(f"{_CS}.get_all_logtype_configs", return_value={})
    @mock.patch(f"{_CS}.get_scan_state", return_value={})
    @mock.patch(f"{_DL}.get_processing_stats", return_value=[])
    @mock.patch(f"{_DL}.get_recent_events", return_value=[])
    @mock.patch(f"{_DL}.validate_config", return_value=[])
    def test_download_returns_attachment(self, mock_vc, mock_re, mock_ps,
                                         mock_ss, mock_lc, mock_dl, mock_cr):
        from GetDebugInfo import main
        resp = main(_make_req({"download": "1"}))
        assert resp.status_code == 200
        assert "attachment" in resp.headers.get("Content-Disposition", "")
        assert resp.headers.get("Content-Disposition", "").startswith('attachment; filename="s247-debug-')
        data = json.loads(resp.get_body())
        assert "generated_at" in data

    @mock.patch(f"{_DL}.clear_events")
    def test_clear_events(self, mock_clear):
        from GetDebugInfo import main
        resp = main(_make_req({"clear": "1"}))
        assert resp.status_code == 200
        data = json.loads(resp.get_body())
        assert data["status"] == "ok"
        mock_clear.assert_called_once()

    @mock.patch(f"{_CS}.get_configured_resources", return_value={})
    @mock.patch(f"{_CS}.get_disabled_log_types", return_value=[])
    @mock.patch(f"{_CS}.get_all_logtype_configs", return_value={})
    @mock.patch(f"{_CS}.get_scan_state", return_value={})
    @mock.patch(f"{_DL}.get_processing_stats", return_value=[])
    @mock.patch(f"{_DL}.get_recent_events", return_value=[])
    @mock.patch(f"{_DL}.validate_config", return_value=[])
    @mock.patch(f"{_DL}.test_s247_connectivity", return_value={
        "base_url": "https://site24x7.com",
        "logtype_supported_ok": True,
        "upload_domain": "logc.site24x7.com",
        "upload_domain_ok": True,
    })
    def test_s247_connectivity_test(self, mock_conn, mock_vc, mock_re, mock_ps,
                                     mock_ss, mock_lc, mock_dl, mock_cr):
        from GetDebugInfo import main
        resp = main(_make_req({"test_s247": "1"}))
        data = json.loads(resp.get_body())
        assert "s247_connectivity" in data
        assert data["s247_connectivity"]["logtype_supported_ok"] is True
        assert data["s247_connectivity"]["upload_domain_ok"] is True

    def test_env_vars_mask_secrets(self):
        """Ensure API key and storage connection are masked."""
        import os
        os.environ["SITE24X7_API_KEY"] = "secret-key-123"
        os.environ["AzureWebJobsStorage"] = "DefaultEndpointsProtocol=..."
        try:
            with mock.patch(f"{_CS}.get_configured_resources", return_value={}), \
                 mock.patch(f"{_CS}.get_disabled_log_types", return_value=[]), \
                 mock.patch(f"{_CS}.get_all_logtype_configs", return_value={}), \
                 mock.patch(f"{_CS}.get_scan_state", return_value={}), \
                 mock.patch(f"{_DL}.get_processing_stats", return_value=[]), \
                 mock.patch(f"{_DL}.get_recent_events", return_value=[]), \
                 mock.patch(f"{_DL}.validate_config", return_value=[]):
                from GetDebugInfo import main
                resp = main(_make_req())
                data = json.loads(resp.get_body())
                assert data["environment"]["SITE24X7_API_KEY"] == "***set***"
                assert data["environment"]["AzureWebJobsStorage"] == "***set***"
                body_str = resp.get_body().decode()
                assert "secret-key-123" not in body_str
                assert "DefaultEndpointsProtocol" not in body_str
        finally:
            os.environ.pop("SITE24X7_API_KEY", None)
            os.environ.pop("AzureWebJobsStorage", None)
