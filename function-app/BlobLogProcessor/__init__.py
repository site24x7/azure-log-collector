"""Timer-triggered function that polls per-region Storage Accounts for diagnostic logs.

Azure Diagnostic Settings writes logs to ``insights-logs-{category}/...`` containers
as **append blobs**. Every flush from Azure becomes a separate *committed block* in
the blob's block list. This processor uses block-list checkpointing (per-blob,
per-block) instead of byte offsets or per-account timestamps:

* For each blob, we read only blocks ``[next_block, len(committed)-1)`` — i.e., we
  always skip the last committed block because Azure may still be appending to it.
* On successful upload, ``next_block`` is advanced to ``len(committed)-1`` so the
  previously-skipped tail is the first block read on the next cycle.
* Blobs are **never deleted** by this function. Retention is enforced by an Azure
  Storage *management policy* (lifecycle rule) provisioned alongside the
  storage account in ``shared/region_manager.py``.

This eliminates two failure modes of the prior timestamp+delete design:

1. Loss: appends made between ``download_blob`` and ``delete_blob`` are no longer
   destroyed (we don't delete).
2. Duplicates: a retried blob no longer re-uploads everything (the block-index
   checkpoint resumes from exactly where the previous run stopped).

Approach mirrors the official site24x7/applogs-azure-storage-function reference
implementation (block-list + skip-last-block).
"""

import os
import json
import logging
import time
import traceback
from datetime import datetime, timedelta, timezone

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.mgmt.storage import StorageManagementClient

logger = logging.getLogger(__name__)

CHECKPOINT_CONTAINER = "s247-checkpoints"
CHECKPOINT_BLOB = "blob-processor-checkpoint.json"
# Garbage-collect per-blob checkpoint entries older than this (matches storage
# lifecycle TTL — blobs deleted by lifecycle rule won't come back).
CHECKPOINT_TTL_DAYS = 14
# Max records per upload batch — prevents memory issues and S247 payload limits
MAX_RECORDS_PER_BATCH = 5000
# Stop processing new blobs when this many seconds remain before function timeout
TIME_BUDGET_RESERVE_SEC = 60
# Warn when blob count with unread blocks exceeds this threshold
BACKLOG_WARN_THRESHOLD = 500
# Skip incremental reads larger than this to prevent OOM/timeout (50 MB)
MAX_READ_BYTES = 50 * 1024 * 1024


def main(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logger.warning("BlobLogProcessor: Timer is past due")

    processing_enabled = os.environ.get("PROCESSING_ENABLED", "true").lower() == "true"
    if not processing_enabled:
        logger.warning("BlobLogProcessor: Processing is DISABLED — skipping")
        return

    try:
        _process_all_regions()
    except Exception as e:
        logger.error("BlobLogProcessor: Unhandled error: %s", str(e))
        try:
            from shared.debug_logger import log_event
            log_event("error", "BlobLogProcessor", f"Unhandled error: {e}",
                      {"traceback": traceback.format_exc()})
        except Exception:
            pass


def _process_all_regions():
    from shared.config_store import get_all_logtype_configs, clear_cache, is_log_type_disabled
    from shared.site24x7_client import Site24x7Client

    clear_cache()

    credential = DefaultAzureCredential()
    subscription_id = os.environ.get("SUBSCRIPTION_IDS", "").split(",")[0].strip()
    resource_group = os.environ.get(
        "RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg")
    )

    if not subscription_id:
        logger.error("BlobLogProcessor: No SUBSCRIPTION_IDS configured")
        return

    all_configs = get_all_logtype_configs()
    general_config_b64 = os.environ.get("S247_GENERAL_LOGTYPE", "")
    general_enabled = os.environ.get("GENERAL_LOGTYPE_ENABLED", "false").lower() == "true"

    if not all_configs and not general_enabled:
        logger.info("BlobLogProcessor: No logtype configs found and general not enabled — nothing to process")
        return

    storage_mgmt = StorageManagementClient(credential, subscription_id)
    regional_accounts = []
    for acct in storage_mgmt.storage_accounts.list_by_resource_group(resource_group):
        tags = acct.tags or {}
        if tags.get("managed-by") == "s247-diag-logs" and \
                tags.get("purpose") in ("diag-logs-regional", "diag-logs-tenant"):
            regional_accounts.append(acct)

    if not regional_accounts:
        logger.info("BlobLogProcessor: No regional storage accounts found — nothing to process")
        return

    logger.info("BlobLogProcessor: Found %d regional storage accounts, %d logtype configs",
                len(regional_accounts), len(all_configs))

    main_conn_str = os.environ.get("AzureWebJobsStorage", "")
    checkpoints = _load_checkpoints(main_conn_str)

    client = Site24x7Client()
    total_stats = {
        "processed": 0, "uploaded": 0, "general": 0, "dropped": 0,
        "blobs_advanced": 0, "blobs_with_unread": 0, "blobs_seen": 0,
        "batches_sent": 0, "bytes_read": 0,
        "time_budget_exhausted": False, "error_blobs": [],
    }
    run_start = time.monotonic()

    for acct in regional_accounts:
        acct_name = acct.name
        region = (acct.tags or {}).get("region", acct.primary_location or "unknown")

        try:
            keys = storage_mgmt.storage_accounts.list_keys(resource_group, acct_name)
            acct_key = keys.keys[0].value
            conn_str = (
                f"DefaultEndpointsProtocol=https;AccountName={acct_name};"
                f"AccountKey={acct_key};EndpointSuffix=core.windows.net"
            )
            blob_service = BlobServiceClient.from_connection_string(conn_str)

            for container in blob_service.list_containers():
                cname = container["name"]
                if not cname.startswith("insights-logs-"):
                    continue

                raw_category = cname.replace("insights-logs-", "")
                category_normalized = raw_category.replace("-", "")
                config_key = f"S247_{category_normalized}"

                # Honor the Log Type Filters "disable" toggle at the processing
                # layer — the one stage every source funnels through. This is
                # what makes disabling effective for sources we don't configure
                # (Entra ID / subscription logs), where we can't stop the logs
                # being written; we just skip forwarding them to Site24x7.
                if is_log_type_disabled(category_normalized):
                    logger.info(
                        "BlobLogProcessor: Log type '%s' is disabled — skipping "
                        "container %s (account: %s)",
                        category_normalized, cname, acct_name,
                    )
                    continue

                source_config_b64 = None
                using_general = False
                if config_key in all_configs:
                    import base64
                    source_config_b64 = base64.b64encode(
                        json.dumps(all_configs[config_key]).encode()
                    ).decode()
                elif general_enabled and general_config_b64:
                    source_config_b64 = general_config_b64
                    using_general = True
                else:
                    # No config — leave blobs in place; lifecycle rule will eventually expire them.
                    logger.warning(
                        "BlobLogProcessor: No S247 config for category '%s' "
                        "(container: %s, account: %s) — logs cannot be forwarded; "
                        "blobs will be aged out by storage lifecycle policy.",
                        raw_category, cname, acct_name,
                    )
                    continue

                container_client = blob_service.get_container_client(cname)

                for blob in container_client.list_blobs():
                    if not blob.name.endswith(".json"):
                        continue

                    elapsed = time.monotonic() - run_start
                    if elapsed > (600 - TIME_BUDGET_RESERVE_SEC):
                        logger.warning(
                            "BlobLogProcessor: Time budget exhausted (%.0fs elapsed) — "
                            "remaining blobs will be processed next cycle", elapsed,
                        )
                        total_stats["time_budget_exhausted"] = True
                        break

                    total_stats["blobs_seen"] += 1

                    cp_key = f"{acct_name}/{cname}/{blob.name}"
                    cp_entry = checkpoints.get(cp_key) or {}
                    next_block = int(cp_entry.get("next_block", 0))

                    blob_client = container_client.get_blob_client(blob.name)

                    try:
                        block_list = blob_client.get_block_list(block_list_type="committed")
                        committed = list(block_list[0]) if isinstance(block_list, tuple) else \
                                    list(getattr(block_list, "committed_blocks", []))
                    except Exception as e:
                        logger.warning(
                            "BlobLogProcessor: get_block_list failed for %s/%s: %s — "
                            "falling back to full read",
                            cname, blob.name, str(e),
                        )
                        committed = None

                    try:
                        if committed is None:
                            # Block-list API not usable (e.g., a put-blob single-shot upload).
                            # Fallback: full read once per ``last_modified``. Sentinel
                            # ``next_block: -1`` marks the entry as full-read mode.
                            cur_lm = (blob.last_modified.isoformat()
                                       if blob.last_modified else "")
                            if (cp_entry.get("next_block") == -1
                                    and cp_entry.get("last_modified")
                                    and cp_entry.get("last_modified") == cur_lm):
                                # Already processed at this last_modified — skip
                                checkpoints[cp_key] = dict(cp_entry, last_seen=_now_iso())
                                continue

                            data, records = _full_read_records(blob_client)
                            total_stats["bytes_read"] += len(data)
                            if records:
                                total_stats["processed"] += len(records)
                                ok = _upload_records(client, source_config_b64, records,
                                                     using_general, total_stats,
                                                     acct_name, cname, blob.name)
                                if ok:
                                    checkpoints[cp_key] = {
                                        "next_block": -1,
                                        "last_seen": _now_iso(),
                                        "last_modified": cur_lm,
                                    }
                                    total_stats["blobs_advanced"] += 1
                                # Failed: leave checkpoint as-is for retry
                            else:
                                checkpoints[cp_key] = {
                                    "next_block": -1,
                                    "last_seen": _now_iso(),
                                    "last_modified": cur_lm,
                                }
                            continue

                        n_committed = len(committed)
                        # Always skip the last committed block (Azure may still be appending to it)
                        upper = max(0, n_committed - 1)

                        if next_block >= upper:
                            # Nothing new to read yet (or only the in-flight tail block exists)
                            if n_committed > next_block:
                                total_stats["blobs_with_unread"] += 1
                            checkpoints[cp_key] = {
                                "next_block": next_block,
                                "last_seen": _now_iso(),
                            }
                            continue

                        # Compute byte range to read
                        start_byte = sum(b.size for b in committed[:next_block])
                        end_byte = sum(b.size for b in committed[:upper])
                        length = end_byte - start_byte
                        if length <= 0:
                            checkpoints[cp_key] = {
                                "next_block": upper,
                                "last_seen": _now_iso(),
                            }
                            continue
                        if length > MAX_READ_BYTES:
                            logger.warning(
                                "BlobLogProcessor: Skipping oversized incremental read for %s/%s "
                                "(%.1f MB > %.0f MB) — checkpoint not advanced",
                                cname, blob.name, length / (1024 * 1024),
                                MAX_READ_BYTES / (1024 * 1024),
                            )
                            total_stats["dropped"] += 1
                            total_stats["error_blobs"].append({
                                "account": acct_name, "container": cname,
                                "blob": blob.name,
                                "error": f"Oversized read: {length/(1024*1024):.1f} MB",
                            })
                            continue

                        stream = blob_client.download_blob(offset=start_byte, length=length)
                        data = stream.readall()
                        total_stats["bytes_read"] += len(data)

                        records = _parse_records(data)
                        if not records:
                            checkpoints[cp_key] = {
                                "next_block": upper,
                                "last_seen": _now_iso(),
                            }
                            continue

                        total_stats["processed"] += len(records)
                        ok = _upload_records(client, source_config_b64, records,
                                             using_general, total_stats,
                                             acct_name, cname, blob.name)
                        if ok:
                            checkpoints[cp_key] = {
                                "next_block": upper,
                                "last_seen": _now_iso(),
                            }
                            total_stats["blobs_advanced"] += 1
                        # If not ok, leave checkpoint unchanged → retry next cycle.

                    except Exception as e:
                        logger.error(
                            "BlobLogProcessor: Error processing blob %s/%s: %s",
                            cname, blob.name, str(e),
                        )
                        total_stats["dropped"] += 1
                        total_stats["error_blobs"].append({
                            "account": acct_name, "container": cname,
                            "blob": blob.name, "error": str(e)[:200],
                        })

                if total_stats["time_budget_exhausted"]:
                    break

        except Exception as e:
            logger.error(
                "BlobLogProcessor: Error processing account %s (%s): %s",
                acct_name, region, str(e),
            )

        if total_stats["time_budget_exhausted"]:
            break

    # GC stale checkpoint entries (blobs already aged out by lifecycle rule)
    checkpoints = _gc_checkpoints(checkpoints, ttl_days=CHECKPOINT_TTL_DAYS)
    _save_checkpoints(main_conn_str, checkpoints)

    run_duration = time.monotonic() - run_start
    total_stats["duration_s"] = round(run_duration, 1)

    logger.info(
        "BlobLogProcessor: Summary — processed=%d, uploaded=%d, general=%d, "
        "dropped=%d, blobs_advanced=%d, blobs_seen=%d, blobs_with_unread=%d, "
        "batches=%d, bytes=%d, duration=%.1fs%s",
        total_stats["processed"],
        total_stats["uploaded"],
        total_stats["general"],
        total_stats["dropped"],
        total_stats["blobs_advanced"],
        total_stats["blobs_seen"],
        total_stats["blobs_with_unread"],
        total_stats["batches_sent"],
        total_stats["bytes_read"],
        run_duration,
        " [TIME BUDGET EXHAUSTED]" if total_stats["time_budget_exhausted"] else "",
    )

    try:
        from shared.debug_logger import save_processing_stats, log_event
        save_processing_stats(dict(total_stats))
        if total_stats["uploaded"] > 0 and total_stats["dropped"] == 0:
            log_event("info", "BlobLogProcessor",
                      f"Processed {total_stats['uploaded']} records in "
                      f"{total_stats['batches_sent']} batches "
                      f"({total_stats['duration_s']}s)",
                      {"stats": dict(total_stats)})
        if total_stats["dropped"] > 0:
            log_event("warning", "BlobLogProcessor",
                      f"Dropped {total_stats['dropped']} records",
                      {"stats": dict(total_stats)})
        if total_stats["blobs_with_unread"] > BACKLOG_WARN_THRESHOLD:
            log_event("warning", "BlobLogProcessor",
                      f"Backlog alert: {total_stats['blobs_with_unread']} blobs with unread blocks "
                      f"(threshold: {BACKLOG_WARN_THRESHOLD}). Processing may be falling behind.",
                      {"blobs_with_unread": total_stats["blobs_with_unread"],
                       "threshold": BACKLOG_WARN_THRESHOLD,
                       "duration_s": total_stats["duration_s"]})
        if total_stats["time_budget_exhausted"]:
            log_event("warning", "BlobLogProcessor",
                      f"Time budget exhausted after {total_stats['duration_s']}s — "
                      "remaining blobs deferred to next cycle",
                      {"stats": dict(total_stats)})
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_records(data: bytes) -> list:
    """Parse Azure diagnostic blob bytes into a list of records.

    Azure diagnostic blobs may be:
    1. Standard JSON: ``{"records": [...]}``
    2. NDJSON: one JSON object per line.

    When reading a partial block range, the slice may begin or end with a stray
    ``,`` (Azure's append-blob artifact between flushes). Strip it before parsing.
    """
    if not data:
        return []
    text = data.decode("utf-8", errors="replace")
    # Strip leading/trailing comma artifacts produced by mid-blob slicing
    text = text.strip()
    if text.startswith(","):
        text = text[1:].lstrip()
    if text.endswith(","):
        text = text[:-1].rstrip()
    if not text:
        return []
    # Try wrapped JSON first
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            recs = payload.get("records")
            if isinstance(recs, list):
                return recs
            return [payload]
        if isinstance(payload, list):
            return payload
    except json.JSONDecodeError:
        pass
    # NDJSON
    records = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _full_read_records(blob_client):
    """Fallback when block-list isn't usable: download the whole blob."""
    data = blob_client.download_blob().readall()
    return data, _parse_records(data)


def _upload_records(client, source_config_b64, records, using_general,
                     stats, acct_name, container, blob_name) -> bool:
    """Upload ``records`` in batches; mutate stats accordingly. Returns True iff
    every batch succeeded."""
    all_ok = True
    for batch_start in range(0, len(records), MAX_RECORDS_PER_BATCH):
        batch = records[batch_start:batch_start + MAX_RECORDS_PER_BATCH]
        success = client.post_logs(source_config_b64, batch)
        stats["batches_sent"] += 1
        if success:
            stats["uploaded"] += len(batch)
            if using_general:
                stats["general"] += len(batch)
        else:
            stats["dropped"] += len(batch)
            stats["error_blobs"].append({
                "account": acct_name, "container": container,
                "blob": blob_name, "error": "post_logs failed",
            })
            all_ok = False
    return all_ok


def _gc_checkpoints(checkpoints: dict, ttl_days: int = CHECKPOINT_TTL_DAYS) -> dict:
    """Drop checkpoint entries whose ``last_seen`` is older than ``ttl_days``.

    Blobs are deleted by Azure's storage-account lifecycle policy after retention,
    so their checkpoint entries become permanently orphaned. Without GC the dict
    grows unbounded.
    """
    if not isinstance(checkpoints, dict):
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    out = {}
    for k, v in checkpoints.items():
        if not isinstance(v, dict):
            continue
        last_seen = v.get("last_seen", "")
        try:
            ts = datetime.fromisoformat(last_seen)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
        except Exception:
            # Keep entries with malformed timestamps so we don't lose state on parse bugs
            pass
        out[k] = v
    return out


def _load_checkpoints(conn_str: str) -> dict:
    data, _etag = _load_checkpoints_with_etag(conn_str)
    return data


def _load_checkpoints_with_etag(conn_str: str):
    """Load checkpoints and return (dict, etag). Creates container if missing."""
    if not conn_str:
        return {}, None
    try:
        blob_service = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service.get_container_client(CHECKPOINT_CONTAINER)
        try:
            container_client.create_container()
        except Exception:
            pass
        stream = container_client.download_blob(CHECKPOINT_BLOB)
        etag = getattr(stream.properties, "etag", None)
        data = json.loads(stream.readall())
        if not isinstance(data, dict):
            return {}, etag
        return data, etag
    except Exception:
        return {}, None


def _save_checkpoints(conn_str: str, checkpoints: dict) -> None:
    """Persist checkpoints with ETag-based merge-on-conflict.

    On concurrent writes we re-fetch the remote, merge per-blob entries with
    ``_merge_checkpoints``, and retry. Per-blob entries take the max
    ``next_block`` and the most recent ``last_seen``.
    """
    if not conn_str:
        return
    try:
        from azure.core import MatchConditions
        blob_service = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service.get_container_client(CHECKPOINT_CONTAINER)
        try:
            container_client.create_container()
        except Exception:
            pass
        blob_client = container_client.get_blob_client(CHECKPOINT_BLOB)

        local = dict(checkpoints)
        existing, etag = _load_checkpoints_with_etag(conn_str)
        merged = _merge_checkpoints(existing, local)

        for attempt in range(5):
            try:
                if etag:
                    blob_client.upload_blob(
                        json.dumps(merged), overwrite=True,
                        etag=etag,
                        match_condition=MatchConditions.IfNotModified,
                    )
                else:
                    blob_client.upload_blob(
                        json.dumps(merged), overwrite=True,
                        match_condition=MatchConditions.IfMissing,
                    )
                return
            except Exception as e:
                msg = str(e)
                if ("ConditionNotMet" in msg or "BlobAlreadyExists" in msg
                        or "412" in msg or "409" in msg):
                    existing, etag = _load_checkpoints_with_etag(conn_str)
                    merged = _merge_checkpoints(existing, local)
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
        logger.error(
            "BlobLogProcessor: Checkpoint save gave up after retries "
            "(concurrent contention)"
        )
    except Exception as e:
        logger.error("BlobLogProcessor: Failed to save checkpoints: %s", str(e))


def _merge_checkpoints(remote, local) -> dict:
    """Merge two per-blob checkpoint dicts.

    Each value is a dict ``{"next_block": int, "last_seen": iso, ...}``. On
    conflict we take the **max** ``next_block`` (so a more-advanced run never
    regresses) and the **latest** ``last_seen`` (so GC works correctly).

    For backwards compatibility, legacy string values (old per-account ISO
    timestamps) are dropped — a fresh per-blob checkpoint will be built on the
    next run. This is safe because the worst case is one cycle of duplicate
    re-reads after the upgrade.
    """
    merged: dict = {}
    if isinstance(remote, dict):
        for k, v in remote.items():
            if isinstance(v, dict):
                merged[k] = dict(v)
    if isinstance(local, dict):
        for k, v in local.items():
            if not isinstance(v, dict):
                continue
            prev = merged.get(k)
            if not isinstance(prev, dict):
                merged[k] = dict(v)
                continue
            out = dict(prev)
            try:
                out["next_block"] = max(int(prev.get("next_block", 0)),
                                          int(v.get("next_block", 0)))
            except (TypeError, ValueError):
                out["next_block"] = v.get("next_block", prev.get("next_block", 0))
            ls_p = prev.get("last_seen", "")
            ls_v = v.get("last_seen", "")
            out["last_seen"] = ls_v if (ls_v and ls_v > ls_p) else (ls_p or ls_v)
            if v.get("last_modified"):
                out["last_modified"] = v["last_modified"]
            elif prev.get("last_modified"):
                out["last_modified"] = prev["last_modified"]
            merged[k] = out
    return merged
