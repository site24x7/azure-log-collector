"""Tests for shared/site24x7_client.py — CircuitBreaker, RateLimiter, and API methods."""

import json
import time
from base64 import b64encode
from unittest.mock import patch, MagicMock

import pytest

from shared.site24x7_client import (
    CircuitBreaker,
    RateLimiter,
    Site24x7Client,
    _get_timestamp,
    _get_json_value,
    _is_filters_matched,
    _apply_masking,
    _apply_hashing,
    _apply_derived_fields,
    _json_log_parser,
)


# ─── CircuitBreaker ─────────────────────────────────────────────────────────


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker()
        assert cb.state == "closed"
        assert cb.can_execute() is True

    def test_stays_closed_below_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        assert cb.can_execute() is True

    def test_opens_at_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == "open"
        assert cb.can_execute() is False

    def test_success_resets(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        cb.record_success()
        assert cb.failure_count == 0
        assert cb.state == "closed"

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb.record_failure()
        assert cb.state == "open"
        time.sleep(0.01)
        assert cb.can_execute() is True
        assert cb.state == "half_open"

    def test_half_open_allows_execution(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0)
        cb.record_failure()
        time.sleep(0.01)
        cb.can_execute()  # transitions to half_open
        assert cb.can_execute() is True  # half_open allows

    def test_open_blocks_before_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=9999)
        cb.record_failure()
        assert cb.can_execute() is False


# ─── RateLimiter ─────────────────────────────────────────────────────────────


class TestRateLimiter:
    def test_initial_tokens(self):
        rl = RateLimiter(rate=10, per=1.0)
        assert rl.tokens == 10.0

    def test_acquire_decrements_tokens(self):
        rl = RateLimiter(rate=100, per=1.0)
        rl.acquire()
        assert rl.tokens < 100

    def test_rapid_acquire_doesnt_go_negative(self):
        rl = RateLimiter(rate=2, per=1.0)
        rl.acquire()
        rl.acquire()
        # Third call should sleep but not crash
        rl.acquire()
        assert rl.tokens >= 0 or rl.tokens == 0


# ─── _get_timestamp ─────────────────────────────────────────────────────────


class TestGetTimestamp:
    def test_valid_timestamp(self):
        ts = _get_timestamp("2024-01-15T10:30:45.123456", "%Y-%m-%dT%H:%M:%S.%f")
        assert ts > 0

    def test_invalid_format(self):
        ts = _get_timestamp("not-a-date", "%Y-%m-%dT%H:%M:%S.%f")
        assert ts == 0

    def test_different_format(self):
        ts = _get_timestamp("2024-01-15 10:30:45", "%Y-%m-%d %H:%M:%S")
        assert ts > 0


# ─── _get_json_value ─────────────────────────────────────────────────────────


class TestGetJsonValue:
    def test_simple_key(self):
        assert _get_json_value({"name": "test"}, "name") == "test"

    def test_missing_key(self):
        assert _get_json_value({"name": "test"}, "missing") is None

    def test_nested_dotpath(self):
        obj = {"properties": {"status": "Active"}}
        assert _get_json_value(obj, "properties.status") == "Active"

    def test_json_object_type(self):
        obj = {"tags": {"env": "prod", "team": "infra"}}
        result = _get_json_value(obj, "tags", "json-object")
        assert isinstance(result, list)
        keys = {r["key"] for r in result}
        assert "env" in keys
        assert "team" in keys

    def test_nested_string_json(self):
        obj = {"properties": '{"inner": "value"}'}
        assert _get_json_value(obj, "properties.inner") == "value"


# ─── _is_filters_matched ────────────────────────────────────────────────────


class TestIsFiltersMatched:
    def test_no_filter_config(self):
        assert _is_filters_matched({"a": "b"}, {}) is True

    def test_matching_include_filter(self):
        config = {
            "filterConfig": {
                "level": {"values": "Error|Warning", "match": True}
            }
        }
        assert _is_filters_matched({"level": "Error"}, config) is True
        assert _is_filters_matched({"level": "Info"}, config) is False

    def test_exclude_filter(self):
        config = {
            "filterConfig": {
                "level": {"values": "Debug", "match": False}
            }
        }
        # match=False with XOR: lines matching regex are EXCLUDED
        assert _is_filters_matched({"level": "Debug"}, config) is False
        assert _is_filters_matched({"level": "Error"}, config) is True


# ─── _apply_masking ─────────────────────────────────────────────────────────

import re


class TestApplyMasking:
    def test_basic_masking(self):
        import re
        line = {"password": "secret123"}
        config = {
            "password": {"regex": re.compile(r"(secret\d+)"), "string": "***"}
        }
        _apply_masking(line, config)
        assert line["password"] == "***"

    def test_no_match(self):
        import re
        line = {"name": "hello"}
        config = {
            "name": {"regex": re.compile(r"(secret\d+)"), "string": "***"}
        }
        _apply_masking(line, config)
        assert line["name"] == "hello"


# ─── _apply_hashing ─────────────────────────────────────────────────────────


class TestApplyHashing:
    def test_basic_hashing(self):
        import re
        import hashlib
        line = {"email": "user@test.com"}
        config = {
            "email": {"regex": re.compile(r"(user@test\.com)")}
        }
        _apply_hashing(line, config)
        expected = hashlib.sha256(b"user@test.com").hexdigest()
        assert line["email"] == expected

    def test_no_match_unchanged(self):
        import re
        line = {"email": "no-match"}
        config = {
            "email": {"regex": re.compile(r"(specific@pattern\.com)")}
        }
        _apply_hashing(line, config)
        assert line["email"] == "no-match"


# ─── _apply_derived_fields ──────────────────────────────────────────────────


class TestApplyDerivedFields:
    def test_extracts_named_groups(self):
        import re
        line = {"message": "user=john action=login"}
        derived = {
            "message": [re.compile(r"user=(?P<username>\w+)")]
        }
        _apply_derived_fields(line, derived)
        assert line["username"] == "john"

    def test_no_match(self):
        import re
        line = {"message": "no match here"}
        derived = {
            "message": [re.compile(r"user=(?P<username>\w+)")]
        }
        _apply_derived_fields(line, derived)
        assert "username" not in line


# ─── _json_log_parser ───────────────────────────────────────────────────────


class TestJsonLogParser:
    def test_basic_parsing(self):
        events = [
            {"time": "2024-01-01T00:00:00.000000", "level": "Error", "message": "fail"}
        ]
        config = {
            "dateFormat": "%Y-%m-%dT%H:%M:%S.%f",
            "dateField": "time",
            "jsonPath": [
                {"name": "level", "key": "level"},
                {"name": "message", "key": "message"},
            ],
        }
        lines, size = _json_log_parser(events, config, None, None, None)
        assert len(lines) == 1
        assert lines[0]["level"] == "Error"
        assert lines[0]["message"] == "fail"
        assert lines[0]["_zl_timestamp"] > 0

    def test_filter_excludes_events(self):
        events = [
            {"time": "2024-01-01T00:00:00.000000", "level": "Debug"},
            {"time": "2024-01-01T00:00:00.000000", "level": "Error"},
        ]
        config = {
            "dateFormat": "%Y-%m-%dT%H:%M:%S.%f",
            "dateField": "time",
            "jsonPath": [{"name": "level", "key": "level"}],
            "filterConfig": {
                "level": {"values": "Error", "match": True}
            },
        }
        lines, _ = _json_log_parser(events, config, None, None, None)
        assert len(lines) == 1
        assert lines[0]["level"] == "Error"

    def test_resourceid_extracts_agent_uid(self):
        events = [
            {
                "time": "2024-01-01T00:00:00.000000",
                "resourceId": "/subscriptions/s/resourceGroups/my-rg/providers/M/T/R",
            }
        ]
        config = {
            "dateFormat": "%Y-%m-%dT%H:%M:%S.%f",
            "dateField": "time",
            "jsonPath": [],
        }
        lines, _ = _json_log_parser(events, config, None, None, None)
        assert len(lines) == 1
        assert lines[0]["s247agentuid"] == "my-rg"

    def test_empty_events(self):
        lines, size = _json_log_parser([], {}, None, None, None)
        assert lines == []
        assert size == 0


# ─── Site24x7Client ──────────────────────────────────────────────────────────


class TestSite24x7Client:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("SITE24X7_API_KEY", "test-key-123")
        monkeypatch.setenv("SITE24X7_BASE_URL", "https://test.site24x7.com")
        return Site24x7Client()

    def test_init_reads_env(self, client):
        assert client.device_key == "test-key-123"
        assert client.s247_base_url == "https://test.site24x7.com"

    def test_no_api_key_returns_none(self, monkeypatch):
        client = Site24x7Client()
        assert client._make_s247_request("/test") is None

    @patch("shared.site24x7_client.urllib.request.urlopen")
    def test_get_supported_log_types_success(self, mock_urlopen, client):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"status": "SUCCESS", "supported_types": [{"logtype": "AuditLogs"}]}
        ).encode()
        mock_urlopen.return_value = mock_resp

        result = client.get_supported_log_types()
        assert result is not None
        assert result["status"] == "SUCCESS"

    @patch("shared.site24x7_client.urllib.request.urlopen")
    def test_get_supported_log_types_failure(self, mock_urlopen, client):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"status": "FAILED"}
        ).encode()
        mock_urlopen.return_value = mock_resp

        result = client.get_supported_log_types()
        assert result is None

    @patch("shared.site24x7_client.urllib.request.urlopen")
    def test_create_log_types_success(self, mock_urlopen, client):
        """create_log_types calls /applog/logtype per category and maps the response."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "status": "SUCCESS",
            "apiUpload": True,
            "logType": "auditlogs",
            "dateField": "time",
            "dateFormat": "yyyy-MM-dd'T'HH:mm:ss.SSS",
            "json_path": [{"name": "time", "key": "time"}],
        }).encode()
        mock_urlopen.return_value = mock_resp

        result = client.create_log_types(["AuditLogs"])
        assert result is not None
        assert len(result) == 1
        assert result[0]["category"] == "S247_AuditLogs"
        config = result[0]["sourceConfig"]
        assert config["apiKey"] == "test-key-123"
        assert config["logType"] == "auditlogs"
        assert config["uploadDomain"] == "logc.site24x7.com"
        assert config["jsonPath"] == [{"name": "time", "key": "time"}]

    @patch("shared.site24x7_client.urllib.request.urlopen")
    def test_create_log_type_single(self, mock_urlopen, client):
        """create_log_type returns a sourceConfig dict for a single category."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "status": "SUCCESS",
            "apiUpload": True,
            "logType": "signinlogs",
            "dateField": "time",
            "dateFormat": "yyyy-MM-dd'T'HH:mm:ss.SSS",
            "json_path": [{"name": "time", "key": "time"}],
            "masking": {"field1": {"regex": "\\d+", "string": "***"}},
            "filterConfig": {"level": {"values": "Error|Warning", "match": True}},
        }).encode()
        mock_urlopen.return_value = mock_resp

        config = client.create_log_type("SignInLogs")
        assert config is not None
        assert config["logType"] == "signinlogs"
        assert config["apiKey"] == "test-key-123"
        assert config["uploadDomain"] == "logc.site24x7.com"
        assert "maskingConfig" in config
        assert "filterConfig" in config

    @patch("shared.site24x7_client.urllib.request.urlopen")
    def test_create_log_type_not_api_upload(self, mock_urlopen, client):
        """Returns None when log type doesn't allow API upload."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "status": "SUCCESS",
            "apiUpload": False,
            "logType": "sometype",
        }).encode()
        mock_urlopen.return_value = mock_resp

        result = client.create_log_type("SomeType")
        assert result is None

    @patch("shared.site24x7_client.urllib.request.urlopen")
    def test_create_log_type_error_status(self, mock_urlopen, client):
        """Returns None when server returns error status."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "status": "ERROR",
            "message": "Log Type Not Found",
        }).encode()
        mock_urlopen.return_value = mock_resp

        result = client.create_log_type("UnknownType")
        assert result is None

    def test_get_upload_domain_mapping(self, client):
        """Upload domain is correctly derived from base URL."""
        client.s247_base_url = "https://www.site24x7.com"
        assert client._get_upload_domain() == "logc.site24x7.com"

        client.s247_base_url = "https://www.site24x7.in"
        assert client._get_upload_domain() == "logc.site24x7.in"

        client.s247_base_url = "https://www.site24x7.eu"
        assert client._get_upload_domain() == "logc.site24x7.eu"

        client.s247_base_url = "https://unknown.domain.com"
        assert client._get_upload_domain() == "logc.site24x7.com"  # fallback

    def test_get_api_base_url_maps_www_to_plus(self, client):
        """API base URL maps www.* to plus.* for servlet calls."""
        client.s247_base_url = "https://www.site24x7.com"
        assert client._get_api_base_url() == "https://plus.site24x7.com"

        client.s247_base_url = "https://www.site24x7.in"
        assert client._get_api_base_url() == "https://plus.site24x7.in"

        client.s247_base_url = "https://www.site24x7.eu"
        assert client._get_api_base_url() == "https://plus.site24x7.eu"

        client.s247_base_url = "https://www.site24x7.net.au"
        assert client._get_api_base_url() == "https://plus.site24x7.net.au"

    def test_get_api_base_url_fallback_for_local(self, client):
        """Local/unknown URLs pass through unchanged."""
        client.s247_base_url = "https://localhost:8443"
        assert client._get_api_base_url() == "https://localhost:8443"

        client.s247_base_url = "https://internal.test.server:8443"
        assert client._get_api_base_url() == "https://internal.test.server:8443"

    def test_create_log_types_empty_list(self, client):
        result = client.create_log_types([])
        assert result == []

    @patch("shared.site24x7_client.urllib.request.urlopen")
    def test_post_logs_circuit_breaker_blocks(self, mock_urlopen, client):
        # Force circuit breaker open
        for _ in range(5):
            client.circuit_breaker.record_failure()
        assert client.post_logs("dummy", []) is False
        mock_urlopen.assert_not_called()

    @patch("shared.site24x7_client.urllib.request.urlopen")
    def test_post_logs_overrides_stale_config(self, mock_urlopen, client):
        """post_logs() overrides uploadDomain and apiKey from stale blob configs."""
        client.device_key = "live_key_123"
        client.s247_base_url = "https://www.site24x7.in"
        stale_config = {
            "apiKey": "old_stale_key",
            "logType": "TestLog",
            "uploadDomain": "old.relay.domain.com",
            "dateFormat": "%Y-%m-%dT%H:%M:%S.%f",
            "dateField": "time",
            "jsonPath": [{"name": "msg", "key": "message"}],
        }
        config_b64 = b64encode(json.dumps(stale_config).encode()).decode()
        events = [{"time": "2024-01-01T00:00:00.000000", "message": "hello"}]

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.getheaders.return_value = [("x-uploadid", "uid123")]
        mock_urlopen.return_value = mock_resp

        result = client.post_logs(config_b64, events)
        assert result is True
        # Verify the upload went to the correct domain (logc.site24x7.in, not old.relay.domain.com)
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "logc.site24x7.in" in req.full_url
        assert "live_key_123" in req.full_url or req.get_header("X-devicekey") == "live_key_123"

    @patch("shared.site24x7_client.urllib.request.urlopen")
    def test_post_logs_success(self, mock_urlopen, client):
        config = {
            "apiKey": "key",
            "logType": "TestLog",
            "uploadDomain": "logc.site24x7.com",
            "dateFormat": "%Y-%m-%dT%H:%M:%S.%f",
            "dateField": "time",
            "jsonPath": [{"name": "msg", "key": "message"}],
        }
        config_b64 = b64encode(json.dumps(config).encode()).decode()
        events = [{"time": "2024-01-01T00:00:00.000000", "message": "hello"}]

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.getheaders.return_value = [("x-uploadid", "uid123")]
        mock_urlopen.return_value = mock_resp

        result = client.post_logs(config_b64, events)
        assert result is True

    def test_general_log_type_config(self, monkeypatch):
        monkeypatch.setenv("S247_GENERAL_LOGTYPE", "base64config")
        monkeypatch.setenv("SITE24X7_API_KEY", "k")
        client = Site24x7Client()
        assert client.get_general_log_type_config() == "base64config"

    def test_general_log_type_config_missing(self, client):
        assert client.get_general_log_type_config() is None
