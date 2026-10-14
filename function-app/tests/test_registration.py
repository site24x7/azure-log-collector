"""Collector self-registration with Site24x7 (shared/registration.py)."""

import json
import logging
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from shared import registration
from shared.site24x7_client import Site24x7Client

SECRET = "fn-key-should-never-be-logged"
SITE_ID = ("/subscriptions/sub-1/resourceGroups/s247-diag-logs-rg"
           "/providers/Microsoft.Web/sites/s247-diag-func-oct14")


@pytest.fixture
def azure_env(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_IDS", "sub-1")
    monkeypatch.setenv("RESOURCE_GROUP_NAME", "s247-diag-logs-rg")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "s247-diag-func-oct14")
    monkeypatch.setenv("WEBSITE_HOSTNAME", "s247-diag-func-oct14.azurewebsites.net")
    monkeypatch.setenv("REGION_NAME", "West Europe")
    monkeypatch.setenv("SITE24X7_API_KEY", "device-key-secret")


@pytest.fixture
def no_state():
    with patch("shared.config_store.get_scan_state", return_value={
            "last_scan_time": "2026-10-14T11:32:59+00:00", "total_resources": 155,
            "active_resources": 152, "ignored_resources": 3, "errors": []}), \
         patch("shared.debug_logger.get_processing_stats", return_value=[
            {"timestamp": "2026-10-14T11:40:00+00:00", "uploaded": 7, "dropped": 1},
            {"timestamp": "2026-10-14T11:30:00+00:00", "uploaded": 3, "dropped": 0}]):
        yield


def test_registration_fields_come_from_the_app_environment(azure_env, no_state):
    with patch.object(registration, "get_function_key", return_value=SECRET):
        fields = registration.build_registration()
    assert fields["functionKey"] == SECRET
    assert fields["resourceUrl"] == "https://s247-diag-func-oct14.azurewebsites.net"
    assert fields["resourceId"] == SITE_ID
    assert (fields["functionAppName"], fields["subscriptionId"], fields["resourceGroup"]) == \
        ("s247-diag-func-oct14", "sub-1", "s247-diag-logs-rg")
    assert fields["region"] == "West Europe"
    status = json.loads(fields["status"])
    assert status["total_resources"] == 155
    assert (status["recent_uploaded"], status["recent_dropped"]) == (10, 1)
    assert status["last_processing_time"] == "2026-10-14T11:40:00+00:00"
    assert SECRET not in fields["status"]


def test_outside_azure_registration_is_skipped(monkeypatch):
    monkeypatch.delenv("WEBSITE_HOSTNAME", raising=False)
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    assert registration.build_registration() is None


def test_list_keys_uses_the_app_resource_and_reads_default_key():
    resp = MagicMock(ok=True)
    resp.json.return_value = {"functionKeys": {"default": SECRET}}
    with patch("azure.identity.DefaultAzureCredential") as cred, \
         patch.object(registration.requests, "post", return_value=resp) as post:
        cred.return_value.get_token.return_value = MagicMock(token="arm-token")
        assert registration.get_function_key(SITE_ID) == SECRET
    url = post.call_args.args[0]
    assert url.startswith(f"https://management.azure.com{SITE_ID}/host/default/listKeys?")


def test_keys_go_in_the_post_body_not_the_url(azure_env):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"], captured["body"] = req.full_url, req.data.decode()
        resp = MagicMock()
        resp.__enter__.return_value.read.return_value = b'{"status":"SUCCESS"}'
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        assert Site24x7Client().register_collector({"functionKey": SECRET, "resourceUrl": "u"})
    assert captured["url"].endswith("/applog/azure/register_collector")
    assert "?" not in captured["url"]
    form = urllib.parse.parse_qs(captured["body"])
    assert form["deviceKey"] == ["device-key-secret"]
    assert form["functionKey"] == [SECRET]


def test_failure_never_raises_or_logs_the_key(azure_env, no_state, caplog):
    caplog.set_level(logging.DEBUG)
    with patch.object(registration, "get_function_key", return_value=SECRET), \
         patch("urllib.request.urlopen", side_effect=OSError(f"boom {SECRET}")):
        assert registration.register_with_site24x7() is False
    assert SECRET not in caplog.text
