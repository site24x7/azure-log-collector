"""GET /api/settings reports the values the collector actually uses when a setting is unset."""

import json

import azure.functions as func

import UpdateSettings


def _get():
    return func.HttpRequest(method="GET", url="/api/settings", body=b"", headers={})


def test_unset_settings_report_runtime_defaults(monkeypatch):
    for key in UpdateSettings.ALLOWED_SETTINGS:
        monkeypatch.delenv(key, raising=False)
    current = json.loads(UpdateSettings.main(_get()).get_body())
    assert current == {"SAFE_DELETE_MAX_AGE_DAYS": 7, "MONITOR_PIPELINE_RESOURCES": False,
                       "AUTO_SCAN_ENABLED": True}


def test_set_settings_are_reported(monkeypatch):
    monkeypatch.setenv("SAFE_DELETE_MAX_AGE_DAYS", "14")
    monkeypatch.setenv("AUTO_SCAN_ENABLED", "false")
    monkeypatch.setenv("MONITOR_PIPELINE_RESOURCES", "true")
    current = json.loads(UpdateSettings.main(_get()).get_body())
    assert current == {"SAFE_DELETE_MAX_AGE_DAYS": 14, "MONITOR_PIPELINE_RESOURCES": True,
                       "AUTO_SCAN_ENABLED": False}


def test_empty_settings_read_like_the_collector(monkeypatch):
    # An empty yes/no setting is off at run time; empty days fall back to 7.
    monkeypatch.setenv("AUTO_SCAN_ENABLED", "")
    monkeypatch.setenv("MONITOR_PIPELINE_RESOURCES", "")
    monkeypatch.setenv("SAFE_DELETE_MAX_AGE_DAYS", "")
    current = json.loads(UpdateSettings.main(_get()).get_body())
    assert current == {"SAFE_DELETE_MAX_AGE_DAYS": 7, "MONITOR_PIPELINE_RESOURCES": False,
                       "AUTO_SCAN_ENABLED": False}
