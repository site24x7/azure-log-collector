"""A scan after an installation, an update or a restart, queued by the first processing run of a worker."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import shared.startup_scan as startup_scan


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


@pytest.fixture(autouse=True)
def fresh_worker(monkeypatch):
    monkeypatch.setattr(startup_scan, "_checked", False)
    monkeypatch.setenv("AUTO_SCAN_ENABLED", "true")
    monkeypatch.delenv("MIN_SCAN_INTERVAL_MINUTES", raising=False)


@pytest.mark.parametrize("state, reason", [
    ({}, "installation"),                                                                # never scanned
    ({"last_scan_time": _ago(5), "collector_version": "1.1.0-beta3"}, "update"),         # scanned by the old version
    ({"last_scan_time": _ago(5)}, "update"),                                             # scanned before versions were recorded
    ({"last_scan_time": _ago(45), "collector_version": "1.1.0-beta4"}, "restart"),
    ({"last_scan_time": _ago(5), "collector_version": "1.1.0-beta4"}, None),             # restart right after a scan
    ({"last_scan_time": _ago(45), "collector_version": "1.1.0-beta3", "in_progress": True}, None),
])
def test_reason(state, reason):
    with patch("shared.config_store.get_scan_state", return_value=state), \
         patch("shared.updater.get_local_version", return_value="1.1.0-beta4"):
        assert startup_scan.startup_scan_reason() == reason


def test_only_the_first_run_of_a_worker_asks():
    with patch("shared.config_store.get_scan_state", return_value={}) as state:
        assert startup_scan.startup_scan_reason() == "installation"
        assert startup_scan.startup_scan_reason() is None
    assert state.call_count == 1


def test_automatic_scan_off_queues_nothing(monkeypatch):
    monkeypatch.setenv("AUTO_SCAN_ENABLED", "false")
    with patch("shared.config_store.get_scan_state", return_value={}):
        assert startup_scan.startup_scan_reason() is None


def test_unreadable_state_queues_nothing():
    with patch("shared.config_store.get_scan_state", side_effect=RuntimeError("blob")):
        assert startup_scan.startup_scan_reason() is None


def test_processor_queues_the_scan_like_scan_now():
    import BlobLogProcessor
    queue = MagicMock()
    with patch("shared.startup_scan.startup_scan_reason", return_value="update"), \
         patch("shared.registration.sync_registration"), \
         patch("shared.updater.confirm_running_version"), \
         patch("shared.config_store.get_processing_enabled", return_value=False):
        BlobLogProcessor.main(MagicMock(past_due=False), queue)
    message = json.loads(queue.set.call_args[0][0])
    assert message["source"] == "update"
    assert datetime.fromisoformat(message["requested_at"])
    binding = json.loads((__import__("pathlib").Path(BlobLogProcessor.__file__).parent / "function.json").read_text())
    out = next(b for b in binding["bindings"] if b["direction"] == "out")
    assert (out["type"], out["queueName"]) == ("queue", "scan-requests")  # the queue ScanWorker reads


@pytest.mark.parametrize("state, skipped", [
    ({"last_scan_started_at": _ago(1)}, True),                       # finished scan started after the request
    ({"in_progress": True, "scan_started_at": _ago(1)}, True),       # running scan started after the request
    ({"last_scan_started_at": _ago(10)}, False),                     # older scan: the request still needs one
    ({"in_progress": True, "scan_started_at": _ago(10),
      "last_scan_started_at": _ago(1)}, False),                      # running scan began before the request
    ({}, False),
])
def test_worker_drops_a_request_an_earlier_scan_covers(state, skipped):
    import ScanWorker
    body = json.dumps({"requested_at": _ago(5), "source": "restart"})
    with patch("shared.config_store.get_scan_state", return_value=state):
        assert ScanWorker._already_scanned_since(body) is skipped


def test_worker_scans_when_the_request_has_no_time():
    import ScanWorker
    with patch("shared.config_store.get_scan_state", return_value={"last_scan_started_at": _ago(1)}):
        assert ScanWorker._already_scanned_since("{}") is False


def test_every_scan_state_records_its_start_and_version():
    # Both the full and the time-budget save: without them a worker start rescans forever ("update").
    from pathlib import Path
    import DiagSettingsManager
    source = Path(DiagSettingsManager.__file__).read_text()
    assert source.count("**scan_record") == source.count("save_scan_state({") + source.count("save_scan_state(scan_state)")
    assert '"collector_version": get_local_version()' in source
