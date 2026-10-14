"""Scan start-up guard: no restart-driven rescans, no app-setting writes."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import DiagSettingsManager


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.mark.parametrize("state, should_scan", [
    ({"last_scan_time": _ago(4), "in_progress": False}, False),   # restart right after a scan
    ({"last_scan_time": _ago(360), "in_progress": False}, True),  # the 6-hourly schedule
    ({}, True),                                                    # first start after deploy
    ({"last_scan_time": _ago(4), "in_progress": True}, True),     # crashed mid-scan
])
def test_startup_runs_scan_only_when_due(monkeypatch, state, should_scan):
    monkeypatch.setenv("AUTO_SCAN_ENABLED", "true")
    monkeypatch.delenv("MIN_SCAN_INTERVAL_MINUTES", raising=False)
    with patch("shared.config_store.get_scan_state", return_value=state), \
         patch("shared.config_store.try_acquire_scan_lock", return_value=True), \
         patch.object(DiagSettingsManager, "run_scan") as run_scan, \
         patch("shared.registration.register_with_site24x7"):
        DiagSettingsManager.main(MagicMock(past_due=False))
    assert run_scan.called is should_scan


def test_scan_never_writes_app_settings():
    # Any app-setting write restarts the app; with runOnStartup that loops.
    source = Path(DiagSettingsManager.__file__).read_text()
    assert "update_app_setting(" not in source
