"""Upload failures: what is retried, what is dropped, and what is never sent twice."""

import json
import urllib.error
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import BlobLogProcessor
from BlobLogProcessor import _process_all_regions, _upload_in_order
from shared.site24x7_client import Site24x7Client
from tests.test_blob_log_processor import _FakeBlobClient, _FakeContainerClient, _block, proc_env  # noqa: F401

KEY = "s247diagtest/insights-logs-test/PT1H.json"
CONFIG = b64encode(json.dumps({
    "apiKey": "key", "logType": "TestLog", "uploadDomain": "logc.site24x7.com",
    "dateFormat": "%Y-%m-%dT%H:%M:%S.%f", "dateField": "time",
    "jsonPath": [{"name": "msg", "key": "message"}],
}).encode()).decode()
EVENTS = [{"time": "2024-01-01T00:00:00.000000", "message": "hello"}]


def _stats():
    return {"uploaded": 0, "general": 0, "dropped": 0, "retrying": 0, "rejected": 0,
            "batches_sent": 0, "error_blobs": [], "upload_refused": False}


def _http_error(code):
    return urllib.error.HTTPError("https://logc.site24x7.com/upload", code, "x", {}, None)


# ─── post_logs: which failures trip the shared circuit breaker ───────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SITE24X7_API_KEY", "test-key-123")
    monkeypatch.setenv("SITE24X7_BASE_URL", "https://test.site24x7.com")
    c = Site24x7Client()
    c.circuit_breaker = MagicMock()
    c.circuit_breaker.can_execute.return_value = True
    c.rate_limiter = MagicMock()
    return c


@pytest.mark.parametrize("code", [400, 404, 413, 415, 422])
def test_refused_payload_does_not_trip_the_breaker(client, code):
    with patch("shared.site24x7_client._KEYED_OPENER.open", side_effect=_http_error(code)):
        assert client.post_logs(CONFIG, EVENTS) is False
    assert client.last_rejected == code
    client.circuit_breaker.record_failure.assert_not_called()
    assert client.upload_refused is False


@pytest.mark.parametrize("code", [401, 403])
def test_refused_device_key_is_retried_and_flagged(client, code):
    with patch("shared.site24x7_client._KEYED_OPENER.open", side_effect=_http_error(code)):
        assert client.post_logs(CONFIG, EVENTS) is False
    assert client.last_rejected == 0                     # retried, not dropped
    client.circuit_breaker.record_failure.assert_called_once()
    assert client.upload_refused is True


@pytest.mark.parametrize("error", [_http_error(503), _http_error(429), urllib.error.URLError("down"), TimeoutError()])
def test_outage_is_retried_and_trips_the_breaker(client, error):
    with patch("shared.site24x7_client._KEYED_OPENER.open", side_effect=error):
        assert client.post_logs(CONFIG, EVENTS) is False
    assert client.last_rejected == 0
    client.circuit_breaker.record_failure.assert_called_once()


def test_broken_log_type_config_does_not_trip_the_breaker(client):
    with patch("shared.site24x7_client._KEYED_OPENER.open") as opener:
        assert client.post_logs("not-base64-json", EVENTS) is False
    opener.assert_not_called()
    client.circuit_breaker.record_failure.assert_not_called()
    assert client.last_rejected == 0                     # kept for retry, not dropped


def test_other_2xx_is_not_success(client):
    response = MagicMock(status=202)
    response.getheaders.return_value = []
    with patch("shared.site24x7_client._KEYED_OPENER.open", return_value=response):
        assert client.post_logs(CONFIG, EVENTS) is False
    client.circuit_breaker.record_success.assert_not_called()


def test_a_success_clears_the_last_refusal(client):
    with patch("shared.site24x7_client._KEYED_OPENER.open", side_effect=_http_error(400)):
        client.post_logs(CONFIG, EVENTS)
    ok = MagicMock(status=200)
    ok.getheaders.return_value = []
    with patch("shared.site24x7_client._KEYED_OPENER.open", return_value=ok):
        assert client.post_logs(CONFIG, EVENTS) is True
    assert client.last_rejected == 0


# ─── _upload_in_order: refused batches are dropped, the rest goes on ─────────

class _ScriptedClient:
    """post_logs answers from a script of (success, rejected_status) per call."""

    def __init__(self, script, refused=False):
        self.script, self.calls = list(script), []
        self.last_unparsed, self.last_rejected, self.upload_refused = 0, 0, refused

    def post_logs(self, cfg, batch):
        self.calls.append(list(batch))
        ok, self.last_rejected = self.script.pop(0) if self.script else (True, 0)
        return ok


def test_refused_batch_is_dropped_and_later_batches_still_go(monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 2)
    client = _ScriptedClient([(False, 400), (True, 0)])
    stats = _stats()
    records = [{"n": i} for i in range(4)]
    assert _upload_in_order(client, "cfg", records, False, stats, "a", "c", "b") == 4
    assert (stats["uploaded"], stats["dropped"], stats["rejected"], stats["retrying"]) == (2, 2, 2, 0)
    assert "HTTP 400" in stats["error_blobs"][0]["error"]


def test_too_large_batch_is_split_until_it_fits(monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 4)
    client = _ScriptedClient([(False, 413), (True, 0), (True, 0)])
    stats = _stats()
    records = [{"n": i} for i in range(4)]
    assert _upload_in_order(client, "cfg", records, False, stats, "a", "c", "b") == 4
    assert [len(c) for c in client.calls] == [4, 2, 2]
    assert (stats["uploaded"], stats["dropped"]) == (4, 0)


def test_single_record_too_large_is_dropped(monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 1)
    client = _ScriptedClient([(False, 413), (True, 0)])
    stats = _stats()
    assert _upload_in_order(client, "cfg", [{"n": 1}, {"n": 2}], False, stats, "a", "c", "b") == 2
    assert (stats["uploaded"], stats["dropped"], stats["rejected"]) == (1, 1, 1)


def test_retryable_failure_stops_in_order_and_is_not_dropped(monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 1)
    client = _ScriptedClient([(True, 0), (False, 0)], refused=True)
    stats = _stats()
    assert _upload_in_order(client, "cfg", [{"n": 1}, {"n": 2}, {"n": 3}], False, stats, "a", "c", "b") == 1
    assert (stats["uploaded"], stats["dropped"], stats["retrying"]) == (1, 0, 2)
    assert stats["upload_refused"] is True
    assert len(client.calls) == 2                        # nothing sent ahead of the failure


# ─── Block and single-shot blobs: nothing sent twice after a partial upload ──

class _NoBlockListClient(_FakeBlobClient):
    def get_block_list(self, block_list_type="committed"):
        raise RuntimeError("block list not available")


def _run(proc_env, blob_client, client, last_modified, checkpoints=None):
    container = _FakeContainerClient([("PT1H.json", blob_client, last_modified)])
    service = MagicMock()
    service.list_containers.return_value = [{"name": "insights-logs-test"}]
    service.get_container_client.return_value = container
    sa = SimpleNamespace(name="s247diagtest", primary_location="eastus",
                         tags={"managed-by": "s247-diag-logs", "purpose": "diag-logs-regional"})
    mgmt = MagicMock()
    mgmt.storage_accounts.list_by_resource_group.return_value = [sa]
    mgmt.storage_accounts.list_keys.return_value = SimpleNamespace(keys=[SimpleNamespace(value="key")])
    saved, stats = {}, {}
    with patch("BlobLogProcessor.DefaultAzureCredential"), \
         patch("BlobLogProcessor.StorageManagementClient", return_value=mgmt), \
         patch("BlobLogProcessor.BlobServiceClient.from_connection_string", return_value=service), \
         patch("BlobLogProcessor._save_checkpoints", side_effect=lambda c, cps: saved.update(cps)), \
         patch("BlobLogProcessor._load_checkpoints", return_value=dict(checkpoints or {})), \
         patch("shared.debug_logger.save_processing_stats", side_effect=lambda s: stats.update(s)), \
         patch("shared.config_store.get_all_logtype_configs", return_value={"S247_test": {"x": 1}}), \
         patch("shared.config_store.clear_cache"), \
         patch("shared.site24x7_client.Site24x7Client", return_value=client):
        _process_all_regions()
    return saved, stats


def _blob(payloads, cls=_FakeBlobClient):
    return cls([_block(len(p)) for p in payloads], b"".join(payloads))


def test_partial_block_upload_resumes_after_the_sent_blocks(proc_env, monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 1)
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    payloads = [b'{"a":1}\n', b'{"a":2}\n', b'{"a":3}\n']
    first = _ScriptedClient([(True, 0), (False, 0)])
    saved, _ = _run(proc_env, _blob(payloads), first, old)
    assert saved[KEY]["next_block"] == 1                 # block 1 sent, blocks 2-3 retried
    second = _ScriptedClient([])
    saved, _ = _run(proc_env, _blob(payloads), second, old, checkpoints=saved)
    assert second.calls == [[{"a": 2}], [{"a": 3}]]      # {"a": 1} is not sent again
    assert saved[KEY]["next_block"] == 3


def test_record_split_across_blocks_falls_back_to_all_or_nothing(proc_env, monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 1)
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    payloads = [b'{"a":1}\n{"a"', b':2}\n']              # a record cut across two blocks
    client = _ScriptedClient([(True, 0), (False, 0)])
    saved, _ = _run(proc_env, _blob(payloads), client, old)
    assert KEY not in saved                              # no safe block boundary to advance to


def test_partial_single_shot_upload_skips_the_sent_records(proc_env, monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 1)
    lm = datetime(2026, 10, 22, tzinfo=timezone.utc)
    payloads = [b'{"a":1}\n{"a":2}\n{"a":3}\n']
    first = _ScriptedClient([(True, 0), (False, 0)])
    saved, _ = _run(proc_env, _blob(payloads, _NoBlockListClient), first, lm)
    assert saved[KEY]["sent_records"] == 1 and saved[KEY]["partial_modified"] == lm.isoformat()
    second = _ScriptedClient([])
    saved, _ = _run(proc_env, _blob(payloads, _NoBlockListClient), second, lm, checkpoints=saved)
    assert second.calls == [[{"a": 2}], [{"a": 3}]]
    assert saved[KEY]["next_block"] == -1 and saved[KEY]["last_modified"] == lm.isoformat()


def test_rewritten_single_shot_blob_is_sent_whole_again(proc_env, monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 1)
    lm = datetime(2026, 10, 22, tzinfo=timezone.utc)
    earlier = {KEY: {"next_block": 0, "partial_modified": "2026-10-21T00:00:00+00:00", "sent_records": 1}}
    client = _ScriptedClient([])
    _run(proc_env, _blob([b'{"a":1}\n{"a":2}\n'], _NoBlockListClient), client, lm, checkpoints=earlier)
    assert client.calls == [[{"a": 1}], [{"a": 2}]]      # a different version: nothing skipped


def test_oversized_part_is_counted_apart_from_records(proc_env, monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_READ_BYTES", 4)
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    _, stats = _run(proc_env, _blob([b'{"a":1}\n', b'{"a":2}\n']), _ScriptedClient([]), old)
    assert stats["skipped_parts"] == 1 and stats["dropped"] == 0


# ─── Review findings (Codex, 348227f) ────────────────────────────────────────

def test_matching_counts_across_a_split_record_are_not_a_boundary(proc_env, monkeypatch):
    """Whole range = 2 records; blocks parsed alone also give 2, but different ones."""
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 1)
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    payloads = [b'{"records":[\n{"id":1},\n{"id":2,"nested":\n', b'{"x":3}\n}\n]}']
    first = _ScriptedClient([(True, 0), (False, 0)])
    saved, _ = _run(proc_env, _blob(payloads), first, old)
    assert first.calls == [[{"id": 1}], [{"id": 2, "nested": {"x": 3}}]]
    assert KEY not in saved                              # no boundary: the range is retried whole
    second = _ScriptedClient([])
    _run(proc_env, _blob(payloads), second, old)
    assert {"id": 2, "nested": {"x": 3}} in [r for c in second.calls for r in c]   # not lost


def test_resume_inside_a_partly_sent_block(proc_env, monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 1)
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    payloads = [b'{"a":1}\n{"a":2}\n{"a":3}\n']          # one block, three records
    saved, _ = _run(proc_env, _blob(payloads), _ScriptedClient([(True, 0), (False, 0)]), old)
    assert saved[KEY] == {"next_block": 0, "skip_records": 1, "last_seen": saved[KEY]["last_seen"]}
    second = _ScriptedClient([])
    saved, _ = _run(proc_env, _blob(payloads), second, old, checkpoints=saved)
    assert second.calls == [[{"a": 2}], [{"a": 3}]]      # {"a": 1} is not sent again
    assert saved[KEY]["next_block"] == 1 and "skip_records" not in saved[KEY]


def _append_blob(payloads):
    blob = _blob(payloads)
    blob.blob_type = "AppendBlob"
    return blob


def test_resume_inside_a_partly_sent_append_envelope(proc_env, monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 1)
    lm = datetime(2026, 10, 22, tzinfo=timezone.utc)
    payloads = [b'{"records":[{"a":1},{"a":2},{"a":3}]}\n']
    saved, _ = _run(proc_env, _append_blob(payloads), _ScriptedClient([(True, 0), (False, 0)]), lm)
    assert saved[KEY]["next_byte"] == 0 and saved[KEY]["skip_records"] == 1
    second = _ScriptedClient([])
    saved, _ = _run(proc_env, _append_blob(payloads), second, lm, checkpoints=saved)
    assert second.calls == [[{"a": 2}], [{"a": 3}]]
    assert saved[KEY]["next_byte"] == len(payloads[0]) and "skip_records" not in saved[KEY]


def test_skip_is_ignored_when_the_position_moved(proc_env, monkeypatch):
    monkeypatch.setattr(BlobLogProcessor, "MAX_RECORDS_PER_BATCH", 1)
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    stale = {KEY: {"next_block": 1, "skip_records": 1, "last_seen": "2026-10-22T00:00:00+00:00"}}
    client = _ScriptedClient([])
    _run(proc_env, _blob([b'{"a":1}\n', b'{"a":2}\n{"a":3}\n']), client, old, checkpoints=stale)
    assert client.calls == [[{"a": 3}]]                  # skip applies to block 1 only


class TestProgressMerge:
    """Concurrent saves keep the entry that got further, with its resume fields."""

    merge = staticmethod(BlobLogProcessor._merge_checkpoints)

    def test_single_shot_progress_keeps_the_larger_count(self):
        remote = {"k": {"partial_modified": "v1", "sent_records": 1, "last_seen": "a"}}
        local = {"k": {"partial_modified": "v1", "sent_records": 2, "last_seen": "b"}}
        assert self.merge(remote, local)["k"]["sent_records"] == 2
        assert self.merge(local, remote)["k"]["sent_records"] == 2

    def test_single_shot_completion_beats_partial_progress(self):
        partial = {"k": {"partial_modified": "v1", "sent_records": 2, "last_seen": "a"}}
        done = {"k": {"next_block": -1, "last_modified": "v1", "last_seen": "b"}}
        for remote, local in ((partial, done), (done, partial)):
            merged = self.merge(remote, local)["k"]
            assert merged["next_block"] == -1 and merged["last_modified"] == "v1"
            assert "sent_records" not in merged

    def test_newer_version_beats_older_completion(self):
        done_v1 = {"k": {"next_block": -1, "last_modified": "2026-10-21", "last_seen": "a"}}
        partial_v2 = {"k": {"partial_modified": "2026-10-22", "sent_records": 1, "last_seen": "b"}}
        assert self.merge(done_v1, partial_v2)["k"]["partial_modified"] == "2026-10-22"

    def test_block_skip_travels_with_its_block(self):
        remote = {"k": {"next_block": 2, "last_seen": "a"}}
        local = {"k": {"next_block": 2, "skip_records": 3, "last_seen": "b"}}
        assert self.merge(remote, local)["k"] == {"next_block": 2, "skip_records": 3, "last_seen": "b"}
        further = {"k": {"next_block": 3, "last_seen": "c"}}
        assert self.merge(local, further)["k"] == {"next_block": 3, "last_seen": "c"}

    def test_append_progress_inside_an_envelope(self):
        remote = {"k": {"next_byte": 10, "skip_records": 1, "append_created": "g", "last_seen": "a"}}
        local = {"k": {"next_byte": 10, "skip_records": 4, "append_created": "g", "last_seen": "b"}}
        assert self.merge(remote, local)["k"]["skip_records"] == 4
        assert self.merge(local, remote)["k"]["skip_records"] == 4


# ─── Requests never run past the run's deadline ──────────────────────────────

def test_no_upload_starts_too_close_to_the_deadline(client):
    import time
    client.upload_deadline = time.monotonic() + 1
    with patch("shared.site24x7_client._KEYED_OPENER.open") as opener:
        assert client.post_logs(CONFIG, EVENTS) is False
    opener.assert_not_called()
    client.circuit_breaker.record_failure.assert_not_called()
    assert client.last_rejected == 0                     # retried next run


def test_upload_timeout_is_capped_to_the_time_left(client):
    import time
    client.upload_deadline = time.monotonic() + 30
    ok = MagicMock(status=200)
    ok.getheaders.return_value = []
    with patch("shared.site24x7_client._KEYED_OPENER.open", return_value=ok) as opener:
        assert client.post_logs(CONFIG, EVENTS) is True
    assert opener.call_args.kwargs["timeout"] <= 30
