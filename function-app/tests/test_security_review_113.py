"""Fixes from the 1.1.2 security review (F-05, F-07, F-19, F-20)."""

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from shared import site24x7_client as sc
from shared.site24x7_client import Site24x7Client

DEVICE_KEY = "ab_0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _fresh_client_state(monkeypatch):
    monkeypatch.setattr(Site24x7Client, "_legacy_get", False)
    monkeypatch.setenv("SITE24X7_API_KEY", DEVICE_KEY)
    monkeypatch.setenv("SITE24X7_BASE_URL", "https://www.site24x7.com")


def _ok(body):
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = json.dumps(body).encode()
    return resp


def _http_error(code):
    return urllib.error.HTTPError("https://plus.site24x7.com/x", code, "x", {}, io.BytesIO(b""))


# F-05: the Device Key goes in a POST body, not the URL

def test_supported_types_are_read_with_a_post_and_the_key_stays_out_of_the_url():
    with patch.object(sc._KEYED_OPENER, "open", return_value=_ok({"status": "SUCCESS", "supported_types": []})) as op:
        assert Site24x7Client().get_supported_log_types()["status"] == "SUCCESS"
    req = op.call_args.args[0]
    assert req.get_method() == "POST"
    assert req.full_url == "https://plus.site24x7.com/applog/azure/logtype_supported"
    assert DEVICE_KEY not in req.full_url and DEVICE_KEY in req.data.decode()


def test_log_type_check_uses_the_azure_post_endpoint():
    with patch.object(sc._KEYED_OPENER, "open", return_value=_ok({"status": "SUCCESS"})) as op:
        Site24x7Client()._make_s247_request("/applog/logtype", {"logType": "auditlogs"})
    req = op.call_args.args[0]
    assert req.get_method() == "POST" and req.full_url.endswith("/applog/azure/logtype")
    assert "logType=auditlogs" in req.data.decode() and "?" not in req.full_url


@pytest.mark.parametrize("code", [400, 404, 405])
def test_a_server_without_the_post_endpoint_falls_back_to_get_once(code):
    calls = []

    def fake_open(req, timeout=None):
        calls.append(req)
        if req.get_method() == "POST":
            raise _http_error(code)
        return _ok({"status": "SUCCESS"})

    with patch.object(sc._KEYED_OPENER, "open", side_effect=fake_open):
        client = Site24x7Client()
        assert client._make_s247_request("/applog/logtype", {"logType": "a"})["status"] == "SUCCESS"
        assert client._make_s247_request("/applog/logtype", {"logType": "b"})["status"] == "SUCCESS"
    # POST, GET, then GET straight away for the rest of the run.
    assert [r.get_method() for r in calls] == ["POST", "GET", "GET"]
    assert calls[1].full_url.startswith("https://plus.site24x7.com/applog/logtype?")


def test_a_rejected_key_does_not_switch_to_get():
    def fake_open(req, timeout=None):
        raise _http_error(400)

    with patch.object(sc._KEYED_OPENER, "open", side_effect=fake_open):
        assert Site24x7Client()._make_s247_request("/applog/azure/logtype_supported") is None
    assert Site24x7Client._legacy_get is False


def test_server_errors_are_not_retried_as_get():
    with patch.object(sc._KEYED_OPENER, "open", side_effect=_http_error(500)) as op:
        assert Site24x7Client()._make_s247_request("/applog/azure/logtype_supported") is None
    assert op.call_count == 1


# F-19: oversized patterns from a log type's config fail that batch only

def test_overlong_config_pattern_is_refused():
    with pytest.raises(ValueError):
        sc._compile_config_pattern("a" * (sc.MAX_CONFIG_PATTERN_LEN + 1))
    assert sc._compile_config_pattern("err(or)?").pattern == "err(or)?"


# F-20: the name-suffix fallback only covers resource types the template creates

def test_suffix_fallback_ignores_other_resource_types(monkeypatch):
    from shared import teardown
    monkeypatch.setenv("WEBSITE_SITE_NAME", "s247-diag-func-qt4xn2")
    monkeypatch.setenv("DIAG_STORAGE_SUFFIX", "qt4xn2")

    def res(name, rtype):
        r = MagicMock()
        r.name, r.type, r.tags, r.location = name, rtype, {"managed-by": "s247-diag-logs"}, "eastus"
        return r

    own = res("s247-diag-func-qt4xn2-plan", "Microsoft.Web/serverFarms")
    vm = res("customer-vm-qt4xn2", "Microsoft.Compute/virtualMachines")
    assert teardown.foreign_resources([own, vm]) == ["customer-vm-qt4xn2 (Microsoft.Compute/virtualMachines)"]


# F-07: HealthCheck no longer lists subscription resources

def test_health_check_ignores_azure_test():
    import HealthCheck
    req = MagicMock()
    req.params = {"azure_test": "1"}
    with patch("azure.identity.DefaultAzureCredential") as cred:
        body = json.loads(HealthCheck.main(req).get_body())
    assert "azure_test" not in body and body["status"] == "alive"
    cred.assert_not_called()
