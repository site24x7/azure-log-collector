"""Tests for BlobLogProcessor block-list checkpointing and merge logic."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from BlobLogProcessor import (
    _gc_checkpoints,
    _merge_checkpoints,
    _parse_records,
    _process_all_regions,
    _upload_records,
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _block(size):
    return SimpleNamespace(size=size)


# --------------------------------------------------------------------------- #
# Merge / GC
# --------------------------------------------------------------------------- #

class TestMergeCheckpoints:
    def test_takes_max_next_block(self):
        remote = {"a/c/b.json": {"next_block": 3, "last_seen": "2026-04-10T00:00:00+00:00"}}
        local = {"a/c/b.json": {"next_block": 5, "last_seen": "2026-04-10T01:00:00+00:00"}}
        merged = _merge_checkpoints(remote, local)
        assert merged["a/c/b.json"]["next_block"] == 5
        assert merged["a/c/b.json"]["last_seen"] == "2026-04-10T01:00:00+00:00"

    def test_keeps_remote_when_remote_advanced_further(self):
        remote = {"a/c/b.json": {"next_block": 7, "last_seen": "2026-04-10T02:00:00+00:00"}}
        local = {"a/c/b.json": {"next_block": 5, "last_seen": "2026-04-10T01:00:00+00:00"}}
        merged = _merge_checkpoints(remote, local)
        assert merged["a/c/b.json"]["next_block"] == 7

    def test_takes_latest_last_seen(self):
        remote = {"k": {"next_block": 1, "last_seen": "2026-04-10T00:00:00+00:00"}}
        local = {"k": {"next_block": 1, "last_seen": "2026-04-11T00:00:00+00:00"}}
        merged = _merge_checkpoints(remote, local)
        assert merged["k"]["last_seen"] == "2026-04-11T00:00:00+00:00"

    def test_preserves_keys_from_both_sides(self):
        remote = {"k1": {"next_block": 1, "last_seen": "2026-04-10T00:00:00+00:00"}}
        local = {"k2": {"next_block": 2, "last_seen": "2026-04-11T00:00:00+00:00"}}
        merged = _merge_checkpoints(remote, local)
        assert set(merged.keys()) == {"k1", "k2"}

    def test_drops_legacy_string_values(self):
        # Old per-account ISO timestamp checkpoints are no longer valid
        remote = {"sa-east": "2026-04-10T00:00:00+00:00"}
        local = {"sa-east/c/b.json": {"next_block": 1, "last_seen": _now_iso()}}
        merged = _merge_checkpoints(remote, local)
        assert "sa-east" not in merged
        assert "sa-east/c/b.json" in merged

    def test_handles_empty_or_none(self):
        assert _merge_checkpoints(None, None) == {}
        assert _merge_checkpoints({}, {"k": {"next_block": 1, "last_seen": "x"}}) == \
               {"k": {"next_block": 1, "last_seen": "x"}}

    def test_carries_last_modified_when_present(self):
        remote = {"k": {"next_block": -1, "last_seen": "2026-04-10T00:00:00+00:00",
                         "last_modified": "2026-04-09T23:59:00+00:00"}}
        local = {"k": {"next_block": -1, "last_seen": "2026-04-11T00:00:00+00:00",
                        "last_modified": "2026-04-10T23:59:00+00:00"}}
        merged = _merge_checkpoints(remote, local)
        assert merged["k"]["last_modified"] == "2026-04-10T23:59:00+00:00"


class TestGcCheckpoints:
    def test_drops_stale_entries(self):
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        fresh = _now_iso()
        cps = {
            "old/k": {"next_block": 1, "last_seen": old},
            "new/k": {"next_block": 1, "last_seen": fresh},
        }
        out = _gc_checkpoints(cps, ttl_days=14)
        assert "old/k" not in out
        assert "new/k" in out

    def test_keeps_entries_with_unparseable_last_seen(self):
        cps = {"k": {"next_block": 1, "last_seen": "garbage"}}
        # Should not drop — better to keep than lose state on a parser bug
        assert _gc_checkpoints(cps, ttl_days=14) == cps

    def test_drops_non_dict_values(self):
        cps = {"legacy_string_value": "2026-04-10T00:00:00+00:00",
               "ok": {"next_block": 1, "last_seen": _now_iso()}}
        out = _gc_checkpoints(cps, ttl_days=14)
        assert list(out.keys()) == ["ok"]


# --------------------------------------------------------------------------- #
# Record parsing — handles partial-block slicing artifacts
# --------------------------------------------------------------------------- #

class TestParseRecords:
    def test_wrapped_records_array(self):
        data = b'{"records":[{"a":1},{"a":2}]}'
        assert _parse_records(data) == [{"a": 1}, {"a": 2}]

    def test_ndjson(self):
        data = b'{"a":1}\n{"a":2}\n{"a":3}\n'
        assert _parse_records(data) == [{"a": 1}, {"a": 2}, {"a": 3}]

    def test_strips_leading_comma_artifact(self):
        # Mid-blob slice may begin with ',' (Azure's separator between flushes)
        data = b',{"a":1}\n{"a":2}\n'
        assert _parse_records(data) == [{"a": 1}, {"a": 2}]

    def test_strips_trailing_comma_artifact(self):
        data = b'{"a":1}\n{"a":2},'
        assert _parse_records(data) == [{"a": 1}, {"a": 2}]

    def test_empty_returns_empty_list(self):
        assert _parse_records(b"") == []
        assert _parse_records(b"  \n  ") == []

    def test_skips_unparseable_lines(self):
        data = b'{"a":1}\nNOT_JSON\n{"a":2}\n'
        assert _parse_records(data) == [{"a": 1}, {"a": 2}]


# --------------------------------------------------------------------------- #
# End-to-end block-list semantics via _process_all_regions
# --------------------------------------------------------------------------- #

class _FakeBlobClient:
    """Minimal stand-in for azure.storage.blob.BlobClient."""

    def __init__(self, blocks, body_bytes):
        self._blocks = blocks
        self._body = body_bytes

    def get_block_list(self, block_list_type="committed"):
        # The real SDK returns (committed, uncommitted) tuple
        return (list(self._blocks), [])

    def download_blob(self, offset=None, length=None):
        if offset is None:
            data = self._body
        else:
            data = self._body[offset:offset + length]

        class _S:
            def __init__(self, d):
                self._d = d
            def readall(self):
                return self._d

        return _S(data)


class _FakeContainerClient:
    def __init__(self, blobs):
        self._blobs = blobs

    def list_blobs(self):
        return [SimpleNamespace(name=n, last_modified=lm)
                for (n, _bc, lm) in self._blobs]

    def get_blob_client(self, name):
        for n, bc, _lm in self._blobs:
            if n == name:
                return bc
        raise KeyError(name)


def _build_blob(blocks_payloads):
    """blocks_payloads = list of bytes objects representing each appended block.
    Returns (blocks_metadata, full_bytes, fake_blob_client)."""
    blocks = [_block(len(b)) for b in blocks_payloads]
    body = b"".join(blocks_payloads)
    return blocks, body, _FakeBlobClient(blocks, body)


@pytest.fixture
def proc_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBSCRIPTION_IDS", "sub-1")
    monkeypatch.setenv("RESOURCE_GROUP_NAME", "rg-test")
    monkeypatch.setenv("PROCESSING_ENABLED", "true")
    monkeypatch.setenv("AzureWebJobsStorage", "")  # disables checkpoint persistence


def _patched_run(proc_env, blob_specs, all_configs=None,
                  initial_checkpoints=None):
    """Execute _process_all_regions with mocked Azure + S247 + checkpoints.

    blob_specs: list of (blob_name, blocks_payload_list, last_modified)
    Returns (post_logs_calls, saved_checkpoints).
    """
    from BlobLogProcessor import _save_checkpoints  # noqa

    fake_blobs = []
    for name, payloads, lm in blob_specs:
        _, _, fbc = _build_blob(payloads)
        fake_blobs.append((name, fbc, lm))

    fake_container = _FakeContainerClient(fake_blobs)

    fake_blob_service = MagicMock()
    fake_blob_service.list_containers.return_value = [{"name": "insights-logs-test"}]
    fake_blob_service.get_container_client.return_value = fake_container

    sa = SimpleNamespace(
        name="s247diagtest",
        primary_location="eastus",
        tags={"managed-by": "s247-diag-logs", "purpose": "diag-logs-regional",
              "region": "eastus"},
    )
    fake_storage_mgmt = MagicMock()
    fake_storage_mgmt.storage_accounts.list_by_resource_group.return_value = [sa]
    fake_storage_mgmt.storage_accounts.list_keys.return_value = SimpleNamespace(
        keys=[SimpleNamespace(value="key")]
    )

    fake_client = MagicMock()
    fake_client.post_logs.return_value = True

    saved = {}

    def _save(_conn, cps):
        saved.update(cps)

    def _load(_conn):
        return dict(initial_checkpoints or {})

    with patch("BlobLogProcessor.DefaultAzureCredential"), \
         patch("BlobLogProcessor.StorageManagementClient", return_value=fake_storage_mgmt), \
         patch("BlobLogProcessor.BlobServiceClient.from_connection_string",
               return_value=fake_blob_service), \
         patch("BlobLogProcessor._save_checkpoints", side_effect=_save), \
         patch("BlobLogProcessor._load_checkpoints", side_effect=_load), \
         patch("shared.config_store.get_all_logtype_configs",
               return_value=all_configs or {"S247_test": {"x": 1}}), \
         patch("shared.config_store.clear_cache"), \
         patch("shared.site24x7_client.Site24x7Client", return_value=fake_client):
        _process_all_regions()

    return fake_client.post_logs.call_args_list, saved


class TestBlockListProcessing:
    def test_skips_last_block_on_first_run(self, proc_env):
        # 3 blocks: only the first 2 should be read (last is in-flight tail).
        payloads = [b'{"a":1}\n', b'{"a":2}\n', b'{"a":3}\n']
        calls, cps = _patched_run(
            proc_env,
            [("PT1H.json", payloads, datetime.now(timezone.utc))],
        )
        assert len(calls) >= 1
        sent = calls[0].args[1]
        sent_a = sorted(r.get("a") for r in sent if "a" in r)
        assert sent_a == [1, 2]
        key = "s247diagtest/insights-logs-test/PT1H.json"
        assert cps[key]["next_block"] == 2

    def test_only_one_block_means_no_read(self, proc_env):
        # A single committed block is the in-flight tail — nothing to read yet.
        payloads = [b'{"a":1}\n']
        calls, cps = _patched_run(
            proc_env,
            [("PT1H.json", payloads, datetime.now(timezone.utc))],
        )
        assert calls == []
        key = "s247diagtest/insights-logs-test/PT1H.json"
        assert cps[key]["next_block"] == 0  # never advanced

    def test_resumes_from_checkpoint(self, proc_env):
        # 5 blocks committed; checkpoint says we already read up to block 2.
        # We should now read blocks [2, 4) — i.e., blocks 2 and 3 (skip last = 4).
        payloads = [b'{"a":1}\n', b'{"a":2}\n',
                    b'{"a":3}\n', b'{"a":4}\n',
                    b'{"a":5}\n']
        key = "s247diagtest/insights-logs-test/PT1H.json"
        calls, cps = _patched_run(
            proc_env,
            [("PT1H.json", payloads, datetime.now(timezone.utc))],
            initial_checkpoints={key: {"next_block": 2, "last_seen": _now_iso()}},
        )
        assert len(calls) >= 1
        sent = calls[0].args[1]
        sent_a = sorted(r.get("a") for r in sent if "a" in r)
        assert sent_a == [3, 4]
        assert cps[key]["next_block"] == 4

    def test_no_checkpoint_advance_on_upload_failure(self, proc_env):
        payloads = [b'{"a":1}\n', b'{"a":2}\n', b'{"a":3}\n']
        # Patch with failing client
        fake_blobs = []
        for name, plds, lm in [("PT1H.json", payloads, datetime.now(timezone.utc))]:
            _, _, fbc = _build_blob(plds)
            fake_blobs.append((name, fbc, lm))
        fake_container = _FakeContainerClient(fake_blobs)
        fake_blob_service = MagicMock()
        fake_blob_service.list_containers.return_value = [{"name": "insights-logs-test"}]
        fake_blob_service.get_container_client.return_value = fake_container

        sa = SimpleNamespace(
            name="s247diagtest", primary_location="eastus",
            tags={"managed-by": "s247-diag-logs", "purpose": "diag-logs-regional"},
        )
        fake_storage_mgmt = MagicMock()
        fake_storage_mgmt.storage_accounts.list_by_resource_group.return_value = [sa]
        fake_storage_mgmt.storage_accounts.list_keys.return_value = SimpleNamespace(
            keys=[SimpleNamespace(value="k")]
        )

        fake_client = MagicMock()
        fake_client.post_logs.return_value = False  # all uploads fail

        saved = {}
        with patch("BlobLogProcessor.DefaultAzureCredential"), \
             patch("BlobLogProcessor.StorageManagementClient", return_value=fake_storage_mgmt), \
             patch("BlobLogProcessor.BlobServiceClient.from_connection_string",
                   return_value=fake_blob_service), \
             patch("BlobLogProcessor._save_checkpoints",
                   side_effect=lambda _c, cps: saved.update(cps)), \
             patch("BlobLogProcessor._load_checkpoints", return_value={}), \
             patch("shared.config_store.get_all_logtype_configs",
                   return_value={"S247_test": {"x": 1}}), \
             patch("shared.config_store.clear_cache"), \
             patch("shared.site24x7_client.Site24x7Client", return_value=fake_client):
            _process_all_regions()

        # Failure → checkpoint must NOT be saved for this blob
        key = "s247diagtest/insights-logs-test/PT1H.json"
        assert key not in saved
