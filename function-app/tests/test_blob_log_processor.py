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
    _read_append_records,
    _upload_records,
    MAX_RECORDS_PER_BATCH,
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
        return [SimpleNamespace(name=n, last_modified=lm, size=len(bc._body),
                                blob_type=getattr(bc, "blob_type", "BlockBlob"),
                                creation_time=datetime(2024, 1, 1, tzinfo=timezone.utc))
                for (n, bc, lm) in self._blobs]

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
    # No configuration storage here: the run's strict read of the disabled list is stubbed.
    monkeypatch.setattr("shared.config_store.get_disabled_log_types", lambda strict=False: [])


def _patched_run(proc_env, blob_specs, all_configs=None,
                  initial_checkpoints=None, sa_purpose="diag-logs-regional",
                  append_blob=False, upload_success=True):
    """Execute _process_all_regions with mocked Azure + S247 + checkpoints.

    blob_specs: list of (blob_name, blocks_payload_list, last_modified)
    sa_purpose: the storage account's ``purpose`` tag (regional or tenant).
    Returns (post_logs_calls, saved_checkpoints).
    """
    from BlobLogProcessor import _save_checkpoints  # noqa

    fake_blobs = []
    for name, payloads, lm in blob_specs:
        _, _, fbc = _build_blob(payloads)
        if append_blob:
            fbc.blob_type = "AppendBlob"
        fake_blobs.append((name, fbc, lm))

    fake_container = _FakeContainerClient(fake_blobs)

    fake_blob_service = MagicMock()
    fake_blob_service.list_containers.return_value = [{"name": "insights-logs-test"}]
    fake_blob_service.get_container_client.return_value = fake_container

    sa = SimpleNamespace(
        name="s247diagtest",
        primary_location="eastus",
        tags={"managed-by": "s247-diag-logs", "purpose": sa_purpose,
              "region": "eastus"},
    )
    fake_storage_mgmt = MagicMock()
    fake_storage_mgmt.storage_accounts.list_by_resource_group.return_value = [sa]
    fake_storage_mgmt.storage_accounts.list_keys.return_value = SimpleNamespace(
        keys=[SimpleNamespace(value="key")]
    )

    fake_client = MagicMock()
    fake_client.post_logs.return_value = upload_success

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


class TestAppendPolling:
    def test_only_new_records_are_sent_after_growth_and_restart(self, proc_env):
        first = b'{"a":1}\n'
        second = b'{"a":2}\n'
        lm = datetime(2026, 10, 14, tzinfo=timezone.utc)
        calls, saved = _patched_run(proc_env, [("b.json", [first], lm)], append_blob=True)
        assert calls[0].args[1] == [{"a": 1}]
        calls, saved = _patched_run(proc_env, [("b.json", [first, second], lm + timedelta(minutes=1))],
                                    append_blob=True, initial_checkpoints=saved)
        assert calls[0].args[1] == [{"a": 2}]
        calls, _ = _patched_run(proc_env, [("b.json", [first, second], lm + timedelta(minutes=1))],
                                append_blob=True, initial_checkpoints=saved)
        assert calls == []

    def test_partial_record_is_retained_until_complete(self, proc_env):
        first = b'{"a":1}\n'
        partial = b'{"a":'
        lm = datetime(2026, 10, 14, tzinfo=timezone.utc)
        calls, saved = _patched_run(proc_env, [("b.json", [first, partial], lm)], append_blob=True)
        assert calls[0].args[1] == [{"a": 1}]
        assert next(iter(saved.values()))["next_byte"] == len(first)
        calls, _ = _patched_run(proc_env, [("b.json", [first, partial, b'2}\n'], lm)],
                                append_blob=True, initial_checkpoints=saved)
        assert calls[0].args[1] == [{"a": 2}]

    def test_failed_upload_does_not_advance_byte_checkpoint(self, proc_env):
        lm = datetime(2026, 10, 14, tzinfo=timezone.utc)
        calls, saved = _patched_run(proc_env, [("b.json", [b'{"a":1}\n'], lm)],
                                    append_blob=True, upload_success=False)
        assert calls
        assert saved == {}

    def test_unchanged_legacy_full_read_checkpoint_migrates_without_redelivery(self, proc_env):
        lm = datetime(2026, 10, 14, tzinfo=timezone.utc)
        old = {"s247diagtest/insights-logs-test/b.json":
               {"next_block": -1, "last_modified": lm.isoformat()}}
        calls, saved = _patched_run(proc_env, [("b.json", [b'{"a":1}\n'], lm)],
                                    append_blob=True, initial_checkpoints=old)
        assert calls == []
        assert next(iter(saved.values()))["next_byte"] == len(b'{"a":1}\n')

    def test_partial_multibyte_json_and_concatenated_envelopes(self):
        first = '{"records":[{"message":"é"}]}'.encode()
        tail = '{"message":"界"}'.encode()
        body = first + tail[:-2]
        _, rows, consumed, _ = _read_append_records(_FakeBlobClient([], body), 0, len(body))
        assert rows == [{"message": "é"}]
        assert consumed == len(first)
        body = first + tail
        _, rows, consumed, _ = _read_append_records(_FakeBlobClient([], body), len(first), len(tail))
        assert rows == [{"message": "界"}]
        assert consumed == len(tail)

    def test_malformed_terminated_record_is_skipped_not_blocking(self, proc_env):
        lm = datetime(2026, 10, 14, tzinfo=timezone.utc)
        calls, saved = _patched_run(proc_env, [("b.json", [b'{"a":1}\n', b'{bad json}\n',
                                                          b'{"a":2}\n'], lm)], append_blob=True)
        assert calls[0].args[1] == [{"a": 1}, {"a": 2}]
        assert next(iter(saved.values()))["next_byte"] == len(b'{"a":1}\n{bad json}\n{"a":2}\n')

    def test_invalid_utf8_keeps_exact_byte_offsets(self):
        body = b'{"m":"\xff"}\n{"a":2}\n'
        _, rows, consumed, skipped = _read_append_records(_FakeBlobClient([], body), 0, len(body))
        assert rows[1] == {"a": 2}
        assert consumed == len(body)
        assert skipped == 0

    def test_unchanged_legacy_checkpoint_skips_every_chunk(self, proc_env, monkeypatch):
        import BlobLogProcessor
        monkeypatch.setattr(BlobLogProcessor, "MAX_READ_BYTES", 10)
        lm = datetime(2026, 10, 14, tzinfo=timezone.utc)
        rows = [b'{"a":1}\n', b'{"a":2}\n', b'{"a":3}\n']
        old = {"s247diagtest/insights-logs-test/b.json":
               {"next_block": -1, "last_modified": lm.isoformat()}}
        calls, saved = _patched_run(proc_env, [("b.json", rows, lm)],
                                    append_blob=True, initial_checkpoints=old)
        assert calls == []
        assert next(iter(saved.values()))["next_byte"] == len(b"".join(rows))
        calls, _ = _patched_run(proc_env, [("b.json", rows + [b'{"a":4}\n'],
                                            lm + timedelta(minutes=1))],
                                append_blob=True, initial_checkpoints=saved)
        assert [c.args[1] for c in calls] == [[{"a": 4}]]

    @pytest.mark.parametrize("body, rows, consumed_all, skipped", [
        # A corrupt record with an unclosed quote fails as "Invalid control character" at
        # its newline (strict JSON), so it is skipped and later records still arrive.
        (b'{"a":"abc\n{"b":1}\n{"c":2}\n', [{"b": 1}, {"c": 2}], True, 1),
        # A genuinely unfinished tail ("Unterminated string", no newline) is kept.
        (b'{"b":1}\n{"a":"abc', [{"b": 1}], False, 0),
        # A pretty-printed envelope cut mid-way is kept whole, not treated as corrupt.
        (b'{"records":[\n {"x":1},\n {"y":"ab', [], False, 0),
    ])
    def test_unclosed_quote_corrupt_line_vs_unfinished_tail(self, body, rows, consumed_all, skipped):
        _, got, consumed, got_skipped = _read_append_records(_FakeBlobClient([], body), 0, len(body))
        assert got == rows
        assert (consumed == len(body)) is consumed_all
        assert got_skipped == skipped

    def test_merge_never_regresses_append_offset(self):
        remote = {"b": {"next_block": -1, "next_byte": 200, "append_created": "2024"}}
        local = {"b": {"next_block": -1, "next_byte": 100, "append_created": "2024"}}
        assert _merge_checkpoints(remote, local)["b"]["next_byte"] == 200

    def test_recreated_blob_uses_new_generation_checkpoint(self):
        remote = {"b": {"next_block": -1, "next_byte": 200, "append_created": "2024"}}
        local = {"b": {"next_block": -1, "next_byte": 20, "append_created": "2025"}}
        assert _merge_checkpoints(remote, local)["b"]["next_byte"] == 20


class TestTenantStorageAccountPolled:
    def test_tenant_tagged_account_is_processed(self, proc_env):
        # A diag-logs-tenant account (dedicated Entra SA) must be polled just
        # like a regional one, so tenant logs landing there get forwarded.
        payloads = [b'{"a":1}\n', b'{"a":2}\n', b'{"a":3}\n']
        calls, _ = _patched_run(
            proc_env,
            [("PT1H.json", payloads, datetime.now(timezone.utc))],
            sa_purpose="diag-logs-tenant",
        )
        assert len(calls) >= 1


class TestDisabledLogTypeSkip:
    def test_disabled_category_container_is_skipped(self, proc_env):
        # The 'test' category (container insights-logs-test) is disabled via the
        # Log Type Filters page — BlobLogProcessor must not forward its blobs,
        # regardless of source (this is what makes disable effective for Entra /
        # subscription logs, whose diagnostic settings we don't control).
        payloads = [b'{"a":1}\n', b'{"a":2}\n', b'{"a":3}\n']
        with patch("shared.config_store.is_log_type_disabled",
                   side_effect=lambda c: c.lower() == "test"):
            calls, cps = _patched_run(
                proc_env,
                [("PT1H.json", payloads, datetime.now(timezone.utc))],
            )
        assert calls == []          # nothing uploaded
        assert cps == {}            # no checkpoint advanced for a skipped container

    def test_enabled_category_still_processed(self, proc_env):
        # Control: with nothing disabled, the same container IS processed.
        payloads = [b'{"a":1}\n', b'{"a":2}\n', b'{"a":3}\n']
        with patch("shared.config_store.is_log_type_disabled", return_value=False):
            calls, _ = _patched_run(
                proc_env,
                [("PT1H.json", payloads, datetime.now(timezone.utc))],
            )
        assert len(calls) >= 1


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



def test_multiline_append_envelope_cut_at_read_limit_is_retained():
    from BlobLogProcessor import _read_append_records
    from unittest.mock import MagicMock
    prefix = b'{\n "records": [{\n "message": "unfinished'
    blob = MagicMock()
    blob.download_blob.return_value.readall.return_value = prefix
    data, records, consumed, skipped = _read_append_records(blob, 0, len(prefix))
    assert data == prefix
    assert records == []
    assert consumed == 0
    assert skipped == 0
    complete = prefix + b'"}]}\n'
    blob.download_blob.return_value.readall.return_value = complete
    _, records, consumed, skipped = _read_append_records(blob, 0, len(complete))
    assert records == [{"message": "unfinished"}]
    assert consumed == len(complete)
    assert skipped == 0


class TestUploadFailureIsRetryNotDrop:
    """A failed upload is re-read next run (checkpoint kept), so it counts as retrying."""

    def _stats(self):
        return {"uploaded": 0, "general": 0, "dropped": 0, "retrying": 0,
                "batches_sent": 0, "error_blobs": []}

    def test_failed_batch_counts_as_retrying(self):
        client = MagicMock()
        client.post_logs.side_effect = [True, False]
        stats = self._stats()
        records = [{"n": i} for i in range(MAX_RECORDS_PER_BATCH + 3)]
        ok = _upload_records(client, "cfg", records, False, stats, "acct", "c", "b.json")
        assert ok is False
        assert (stats["uploaded"], stats["retrying"], stats["dropped"]) == (MAX_RECORDS_PER_BATCH, 3, 0)

    def test_successful_upload_has_nothing_retrying(self):
        client = MagicMock()
        client.post_logs.return_value = True
        stats = self._stats()
        assert _upload_records(client, "cfg", [{"n": 1}], False, stats, "acct", "c", "b.json")
        assert (stats["uploaded"], stats["retrying"], stats["dropped"]) == (1, 0, 0)


class TestDisabledEntraCategories:
    """A disabled Entra category must not reach Site24x7 through the general log type."""

    def test_disabled_entra_categories_are_listed(self):
        from BlobLogProcessor import _disabled_entra_categories
        states = {"signinlogs": {"enabled": False}, "auditlogs": {"enabled": True},
                  "notentra": {"enabled": False}}
        with patch("shared.config_store.get_entra_logtype_states", return_value=states):
            assert _disabled_entra_categories() == {"signinlogs"}

    def test_unreadable_state_means_unknown(self):
        from BlobLogProcessor import _disabled_entra_categories
        with patch("shared.config_store.get_entra_logtype_states", side_effect=RuntimeError("down")):
            assert _disabled_entra_categories() is None



def test_unreadable_disabled_list_skips_the_run(monkeypatch):
    """Disabled categories must not be sent because their list could not be read."""
    from BlobLogProcessor import _process_all_regions
    monkeypatch.setenv("SUBSCRIPTION_IDS", "sub-1")

    def unreadable(strict=False):
        raise RuntimeError("throttled")
    monkeypatch.setattr("shared.config_store.get_disabled_log_types", unreadable)
    with patch("BlobLogProcessor.DefaultAzureCredential") as cred:
        _process_all_regions()
    cred.assert_not_called()   # stopped before touching any storage account
