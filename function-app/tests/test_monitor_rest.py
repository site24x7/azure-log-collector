"""Tests for shared/monitor_rest.py and the MONITOR_CLIENT selection."""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from shared import monitor_rest
from shared.azure_manager import AzureManager, _type_not_supported
from shared.monitor_rest import MonitorRestClient, build_body

RID = "/subscriptions/s/resourceGroups/rg/providers/M/T/R"


def resp(status=200, body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = body if body is not None else {}
    return r


@pytest.fixture
def client():
    cred = MagicMock()
    cred.get_token.return_value = SimpleNamespace(token="tok", expires_on=9e12)
    return MonitorRestClient(cred)


@pytest.fixture
def req():
    with patch("shared.monitor_rest.requests.Session.request") as m, \
            patch("shared.monitor_rest.time.sleep") as sl:
        m.sleep = sl
        yield m


def test_list_parsing(client, req):
    req.return_value = resp(200, {"value": [
        {"name": "A", "properties": {"categoryType": "Logs"}},
        {"name": "AllMetrics", "properties": {"categoryType": "Metrics"}},
    ]})
    out = client.diagnostic_settings_category.list(RID)
    assert [(c.name, c.category_type) for c in out.value] == [("A", "Logs"), ("AllMetrics", "Metrics")]
    args, kw = req.call_args
    assert args == ("GET", "https://management.azure.com" + RID +
                    "/providers/Microsoft.Insights/diagnosticSettingsCategories?api-version=2021-05-01-preview")
    assert kw["allow_redirects"] is False
    assert kw["timeout"] == (10, 60)
    assert kw["headers"]["Authorization"] == "Bearer tok"


def test_get_parsing_category_and_group(client, req):
    req.return_value = resp(200, {"id": "i", "name": "n", "properties": {
        "storageAccountId": "/sa",
        "logs": [{"category": "C", "enabled": True},
                 {"categoryGroup": "allLogs", "enabled": False}]}})
    s = client.diagnostic_settings.get(RID, "my name/x")
    assert s.storage_account_id == "/sa" and s.id == "i" and s.name == "n"
    assert (s.logs[0].category, s.logs[0].enabled) == ("C", True)
    assert (s.logs[1].category_group, s.logs[1].category, s.logs[1].enabled) == ("allLogs", None, False)
    assert req.call_args[0][1].endswith("/diagnosticSettings/my%20name%2Fx?api-version=2021-05-01-preview")


def test_put_body_from_dict_snake_case(client, req):
    req.return_value = resp(200, {"properties": {}})
    client.diagnostic_settings.create_or_update(
        resource_uri=RID, name="n",
        parameters={"storage_account_id": "/sa", "logs": [
            {"category": "C", "enabled": True},
            {"category_group": "allLogs", "enabled": True}], "metrics": []})
    args, kw = req.call_args
    assert args[0] == "PUT"
    assert kw["json"] == {"properties": {"storageAccountId": "/sa", "metrics": [], "logs": [
        {"category": "C", "enabled": True}, {"categoryGroup": "allLogs", "enabled": True}]}}


def test_put_body_from_model_objects():
    params = SimpleNamespace(
        storage_account_id="/sa", metrics=[],
        logs=[SimpleNamespace(category="C", category_group=None, enabled=True),
              SimpleNamespace(category=None, category_group="allLogs", enabled=True)])
    assert build_body(params) == {"properties": {"storageAccountId": "/sa", "metrics": [], "logs": [
        {"category": "C", "enabled": True}, {"categoryGroup": "allLogs", "enabled": True}]}}


def test_delete_ok_and_404_idempotent(client, req):
    req.return_value = resp(200)
    assert client.diagnostic_settings.delete(RID, "n") is None
    assert req.call_args[0][0] == "DELETE"
    req.return_value = resp(404, {"error": {"code": "ResourceNotFound"}})
    assert client.diagnostic_settings.delete(RID, "n") is None


def test_get_404_is_resource_not_found(client, req):
    req.return_value = resp(404, {"error": {"code": "ResourceNotFound", "message": "gone"}})
    with pytest.raises(ResourceNotFoundError) as ei:
        client.diagnostic_settings.get(RID, "n")
    assert ei.value.status_code == 404


def test_400_type_not_supported_recognized(client, req):
    req.return_value = resp(400, {"error": {"code": "ResourceTypeNotSupported", "message": "x"}})
    with pytest.raises(HttpResponseError) as ei:
        client.diagnostic_settings_category.list(RID)
    assert _type_not_supported(ei.value)
    assert not isinstance(ei.value, ResourceNotFoundError)


def test_403_surfaces(client, req):
    req.return_value = resp(403, {"error": {"code": "AuthorizationFailed", "message": "no"}})
    with pytest.raises(HttpResponseError) as ei:
        client.diagnostic_settings_category.list(RID)
    assert ei.value.status_code == 403 and not _type_not_supported(ei.value)
    assert req.call_count == 1


def test_429_retries_then_succeeds(client, req):
    req.side_effect = [resp(429, headers={"Retry-After": "2"}),
                       resp(503, headers={"Retry-After": "999"}),
                       resp(200, {"value": []})]
    assert client.diagnostic_settings_category.list(RID).value == []
    assert req.call_count == 3
    assert [c.args[0] for c in req.sleep.call_args_list] == [2.0, 30.0]


def test_retries_are_bounded(client, req):
    req.return_value = resp(500)
    with pytest.raises(HttpResponseError) as ei:
        client.diagnostic_settings_category.list(RID)
    assert ei.value.status_code == 500 and req.call_count == monitor_rest.MAX_ATTEMPTS


def test_connection_errors_are_retried(client, req):
    import requests
    req.side_effect = [requests.ConnectTimeout("connect timed out"), requests.ConnectionError("reset"),
                       resp(200, {"value": []})]
    assert client.diagnostic_settings_category.list(RID).value == []
    assert req.call_count == 3


def test_connection_errors_are_bounded(client, req):
    import requests
    req.side_effect = requests.ConnectTimeout("connect timed out")
    with pytest.raises(requests.ConnectTimeout):
        client.diagnostic_settings_category.list(RID)
    assert req.call_count == monitor_rest.MAX_ATTEMPTS


def test_one_pooled_session_is_reused(client, req):
    req.return_value = resp(200, {"value": []})
    client.diagnostic_settings_category.list(RID)
    client.diagnostic_settings_category.list(RID)
    adapter = client._session.get_adapter("https://management.azure.com")
    assert adapter._pool_maxsize >= 10  # the removal and scan pools use 10 threads


def test_token_not_logged(client, req, caplog):
    req.side_effect = [resp(429), resp(200, {"value": []})]
    with caplog.at_level(logging.DEBUG):
        client.diagnostic_settings_category.list(RID)
    assert "tok" not in caplog.text.replace("token", "")


# --- selection ---------------------------------------------------------------


@pytest.fixture
def manager():
    with patch("shared.azure_manager.DefaultAzureCredential"):
        return AzureManager()


def test_default_is_rest(manager, monkeypatch):
    monkeypatch.delenv("MONITOR_CLIENT", raising=False)
    assert isinstance(manager._get_monitor_client("s"), MonitorRestClient)


def test_sdk_selected_case_insensitive(manager, monkeypatch):
    monkeypatch.setenv("MONITOR_CLIENT", "SDK")
    with patch("shared.azure_manager.MonitorManagementClient") as cls:
        assert manager._get_monitor_client("s") is cls.return_value


def test_other_value_is_rest(manager, monkeypatch):
    monkeypatch.setenv("MONITOR_CLIENT", "rest")
    assert isinstance(manager._get_monitor_client("s"), MonitorRestClient)


def test_sdk_unavailable_falls_back_with_one_warning(manager, monkeypatch, caplog):
    monkeypatch.setenv("MONITOR_CLIENT", "sdk")
    monkeypatch.setattr(monitor_rest, "_sdk_warned", False)
    with patch("shared.azure_manager.MonitorManagementClient", None), \
            caplog.at_level(logging.WARNING):
        assert isinstance(manager._get_monitor_client("a"), MonitorRestClient)
        assert isinstance(manager._get_monitor_client("b"), MonitorRestClient)
    assert caplog.text.count("MONITOR_CLIENT=sdk") == 1
