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
import codecs
import json
import logging
import time
import traceback
from datetime import datetime, timedelta, timezone

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from azure.mgmt.storage import StorageManagementClient

from shared.entra_config import get_entra_normalized_categories

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
# A block blob not modified for this long is complete: its last block is read too.
BLOCK_BLOB_QUIET_MINUTES = 90
# Upper bound on one upload's uncompressed records, whatever their count.
MAX_BATCH_BYTES = 4 * 1024 * 1024
# Entra ID categories: switched off in their own state, not the disabled list.
_ENTRA_NORMALIZED = frozenset(get_entra_normalized_categories())


def main(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logger.warning("BlobLogProcessor: Timer is past due")

    # Report to Site24x7 after a restart and when the function key was rotated
    # (cheap: at most one ARM key read per worker every 15 minutes). Runs even
    # while processing is paused, so Site24x7 keeps a working key.
    try:
        from shared.registration import sync_registration
        sync_registration()
    except Exception as e:
        logger.warning("BlobLogProcessor: registration check failed: %s", type(e).__name__)
    try:
        from shared.updater import confirm_running_version
        confirm_running_version()
    except Exception as e:
        logger.warning("BlobLogProcessor: update confirmation failed: %s", type(e).__name__)

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


def _disabled_entra_categories():
    """Normalized Entra categories switched off on the dashboard, or None when
    their state cannot be read (then every Entra category is skipped for this
    run; its blobs stay and are read next run)."""
    from shared.config_store import get_entra_logtype_states
    try:
        states = get_entra_logtype_states(strict=True)
    except Exception as e:
        logger.warning("BlobLogProcessor: Entra log type state unreadable (%s)", type(e).__name__)
        return None
    return {name for name, state in states.items()
            if name in _ENTRA_NORMALIZED and state.get("enabled") is False}


def _process_all_regions():
    from shared.config_store import (get_all_logtype_configs, clear_cache, is_log_type_disabled,
                                     get_disabled_log_types)
    from shared.site24x7_client import Site24x7Client

    clear_cache()
    # Read the switched-off categories first and strictly: if the list cannot be
    # read, skip this run (the blobs stay and are read next run) rather than
    # send categories the user turned off.
    try:
        get_disabled_log_types(strict=True)
    except Exception as e:
        logger.warning("BlobLogProcessor: disabled log types unreadable (%s) — skipping this run",
                       type(e).__name__)
        return

    credential = DefaultAzureCredential()
    subscription_id = os.environ.get("SUBSCRIPTION_IDS", "").split(",")[0].strip()
    resource_group = os.environ.get(
        "RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg")
    )

    if not subscription_id:
        logger.error("BlobLogProcessor: No SUBSCRIPTION_IDS configured")
        return

    all_configs = get_all_logtype_configs()
    disabled_entra = _disabled_entra_categories()
    general_config_b64 = os.environ.get("S247_GENERAL_LOGTYPE", "")
    general_enabled = os.environ.get("GENERAL_LOGTYPE_ENABLED", "false").lower() == "true"

    if not all_configs and not general_enabled:
        logger.info("BlobLogProcessor: No logtype configs found and general not enabled — nothing to process")
        return

    storage_mgmt = StorageManagementClient(credential, subscription_id)
    regional_accounts = []
    from shared.region_manager import is_own_account
    for acct in storage_mgmt.storage_accounts.list_by_resource_group(resource_group):
        purpose = (acct.tags or {}).get("purpose", "")
        # Only this collector's accounts: another collector sharing the group
        # sends with its own Device Key.
        if purpose in ("diag-logs-regional", "diag-logs-tenant") and \
                is_own_account(acct.name, acct.tags, purpose, acct.primary_location or ""):
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
        # dropped = records that will never be sent (malformed, unparseable, refused by
        # Site24x7); skipped_parts = blobs / blocks too large to read (their record count
        # is unknown, so they are not added to dropped); retrying = records whose upload
        # failed this run and are re-read next run.
        "processed": 0, "uploaded": 0, "general": 0, "dropped": 0, "retrying": 0,
        "rejected": 0, "skipped_parts": 0, "upload_refused": False,
        "blobs_advanced": 0, "blobs_with_unread": 0, "blobs_seen": 0,
        "batches_sent": 0, "bytes_read": 0,
        "time_budget_exhausted": False, "error_blobs": [],
    }
    run_start = time.monotonic()
    # Uploads stop at this point so the run ends (and saves checkpoints) in time;
    # the client also caps each request's timeout to the time left.
    total_stats["deadline"] = run_start + 600 - TIME_BUDGET_RESERVE_SEC
    client.upload_deadline = total_stats["deadline"]

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
                # Entra categories are switched off in their own state (the
                # general log type fallback below would otherwise still send them).
                if category_normalized in _ENTRA_NORMALIZED and (
                        disabled_entra is None or category_normalized in disabled_entra):
                    logger.info(
                        "BlobLogProcessor: Entra log type '%s' is disabled or its state is "
                        "unreadable — skipping container %s (account: %s)",
                        category_normalized, cname, acct_name,
                    )
                    continue
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
                    blob_type = getattr(blob, "blob_type", "")
                    is_append = getattr(blob_type, "value", blob_type) == "AppendBlob"

                    try:
                        if is_append:
                            committed = None
                        else:
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
                        if is_append:
                            created = getattr(blob, "creation_time", None)
                            generation = created.isoformat() if isinstance(created, datetime) else ""
                            offset = int(cp_entry.get("next_byte", 0))
                            # A recreated or shrunk blob starts over: neither the old offset nor
                            # the records counted inside its envelope apply to the new content.
                            restarted = False
                            if (generation and cp_entry.get("append_created")
                                    and generation != cp_entry["append_created"]):
                                offset, restarted = 0, True
                            size = int(blob.size)
                            if offset > size:
                                offset, restarted = 0, True
                            cur_lm = blob.last_modified.isoformat() if blob.last_modified else ""
                            # Upgrade unchanged legacy full-read checkpoints without redelivery.
                            legacy_done = ("next_byte" not in cp_entry
                                           and cp_entry.get("next_block") == -1
                                           and cp_entry.get("last_modified") == cur_lm)
                            if legacy_done:
                                # The old full read already delivered the whole blob, so
                                # walk every chunk to a record boundary without sending.
                                consumed = 0
                                records = []
                                while offset + consumed < size:
                                    data, _, step, _ = _read_append_records(
                                        blob_client, offset + consumed,
                                        min(size - offset - consumed, MAX_READ_BYTES))
                                    total_stats["bytes_read"] += len(data)
                                    if not step:
                                        break
                                    consumed += step
                            else:
                                data, records, consumed, skipped, boundaries = _read_append_records(
                                    blob_client, offset, min(size - offset, MAX_READ_BYTES),
                                    with_boundaries=True)
                                total_stats["bytes_read"] += len(data)
                                if skipped:
                                    total_stats["dropped"] += skipped
                                    total_stats["error_blobs"].append({
                                        "account": acct_name, "container": cname,
                                        "blob": blob.name,
                                        "error": f"Skipped {skipped} malformed record(s)",
                                    })
                            if records:
                                # Records of the first envelope an earlier run already handled
                                # (appended bytes never change, so they are the same records).
                                skip = 0 if restarted else _resume_skip(cp_entry, "next_byte", offset, len(records))
                                total_stats["processed"] += len(records) - skip
                                sent = skip + _upload_in_order(client, source_config_b64, records[skip:],
                                                               using_general, total_stats,
                                                               acct_name, cname, blob.name)
                                if sent < len(records):
                                    # Advance exactly past the envelopes that were fully handled and
                                    # count the records handled in the next one, so nothing already
                                    # delivered is sent again (nothing new handled: left as it was).
                                    done_n, done_b = max([(n, b) for n, b in boundaries if n <= sent], default=(0, 0))
                                    if sent == skip:
                                        continue
                                    checkpoints[cp_key] = {
                                        "next_block": -1, "next_byte": offset + done_b,
                                        "skip_records": sent - done_n,
                                        "append_created": generation, "last_seen": _now_iso(),
                                        "last_modified": cur_lm,
                                    }
                                    continue
                            checkpoints[cp_key] = {
                                "next_block": -1, "next_byte": offset + consumed,
                                "append_created": generation, "last_seen": _now_iso(),
                                "last_modified": cur_lm,
                            }
                            if consumed:
                                total_stats["blobs_advanced"] += 1
                            continue

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

                            if (blob.size or 0) > MAX_READ_BYTES:
                                # Too big to read whole; recorded and skipped so it does
                                # not stall the run on every poll.
                                total_stats["skipped_parts"] += 1
                                total_stats["error_blobs"].append({
                                    "account": acct_name, "container": cname, "blob": blob.name,
                                    "error": f"Blob too large to read whole: {(blob.size or 0)/(1024*1024):.1f} MB",
                                })
                                checkpoints[cp_key] = {"next_block": -1, "last_seen": _now_iso(),
                                                       "last_modified": cur_lm}
                                continue
                            data, records = _full_read_records(blob_client)
                            total_stats["bytes_read"] += len(data)
                            # Records already sent from this same version of the blob
                            # (an earlier run stopped part-way) are not sent again.
                            already = (cp_entry.get("sent_records", 0)
                                       if cp_entry.get("partial_modified") == cur_lm else 0)
                            already = already if isinstance(already, int) and 0 <= already <= len(records) else 0
                            records = records[already:]
                            if records:
                                total_stats["processed"] += len(records)
                                sent = _upload_in_order(client, source_config_b64, records,
                                                        using_general, total_stats,
                                                        acct_name, cname, blob.name)
                                if sent == len(records):
                                    checkpoints[cp_key] = {
                                        "next_block": -1,
                                        "last_seen": _now_iso(),
                                        "last_modified": cur_lm,
                                    }
                                    total_stats["blobs_advanced"] += 1
                                elif sent:
                                    # No full-read marker, so the blob is read again; the next run
                                    # resumes after the records this version already handled.
                                    checkpoints[cp_key] = {
                                        "partial_modified": cur_lm, "sent_records": already + sent,
                                        "last_seen": _now_iso(),
                                    }
                                # Nothing sent: leave checkpoint as-is for retry
                            else:
                                checkpoints[cp_key] = {
                                    "next_block": -1,
                                    "last_seen": _now_iso(),
                                    "last_modified": cur_lm,
                                }
                            continue

                        n_committed = len(committed)
                        # Skip the last committed block while Azure may still append to it;
                        # once the blob has been quiet long enough it is complete, and the
                        # last block is read too (else it was lost when retention removed it).
                        quiet = bool(blob.last_modified) and (
                            datetime.now(timezone.utc) - blob.last_modified
                            > timedelta(minutes=BLOCK_BLOB_QUIET_MINUTES))
                        upper = n_committed if quiet else max(0, n_committed - 1)

                        if next_block >= upper:
                            # Nothing new to read yet (or only the in-flight tail block exists)
                            if n_committed > next_block:
                                total_stats["blobs_with_unread"] += 1
                            checkpoints[cp_key] = {
                                "next_block": next_block,
                                "last_seen": _now_iso(),
                            }
                            continue

                        # Read whole blocks up to MAX_READ_BYTES this run; the rest
                        # follows on the next runs (a large backlog no longer stalls).
                        end_block, length = next_block, 0
                        while end_block < upper and length + committed[end_block].size <= MAX_READ_BYTES:
                            length += committed[end_block].size
                            end_block += 1
                        if end_block == next_block:
                            # One block larger than the limit: recorded and skipped.
                            logger.warning(
                                "BlobLogProcessor: Skipping one oversized block of %s/%s (%.1f MB)",
                                cname, blob.name, committed[next_block].size / (1024 * 1024),
                            )
                            total_stats["skipped_parts"] += 1
                            total_stats["error_blobs"].append({
                                "account": acct_name, "container": cname, "blob": blob.name,
                                "error": f"Oversized block: {committed[next_block].size/(1024*1024):.1f} MB",
                            })
                            checkpoints[cp_key] = {"next_block": next_block + 1, "last_seen": _now_iso()}
                            continue
                        if end_block < upper:
                            total_stats["blobs_with_unread"] += 1
                        upper = end_block
                        start_byte = sum(b.size for b in committed[:next_block])

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

                        boundaries = _block_boundaries(data, committed, next_block, upper, records)
                        # Records an earlier run handled from this block; only trusted when every
                        # block parses on its own, so the first block's records are the same ones.
                        skip = _resume_skip(cp_entry, "next_block", next_block, len(records)) if boundaries else 0
                        total_stats["processed"] += len(records) - skip
                        sent = skip + _upload_in_order(client, source_config_b64, records[skip:],
                                                       using_general, total_stats,
                                                       acct_name, cname, blob.name)
                        if sent == len(records):
                            checkpoints[cp_key] = {
                                "next_block": upper,
                                "last_seen": _now_iso(),
                            }
                            total_stats["blobs_advanced"] += 1
                        elif boundaries and sent > skip:
                            # Advance past the blocks whose records were all handled and count the
                            # records handled in the next one, so nothing is sent again.
                            done_n, done_blk = max([(n, blk) for n, blk in boundaries if n <= sent],
                                                   default=(0, next_block))
                            checkpoints[cp_key] = {"next_block": done_blk, "skip_records": sent - done_n,
                                                   "last_seen": _now_iso()}
                        # A record spans blocks: no safe boundary inside the range, so the whole
                        # range is retried next cycle (records may arrive twice, none is lost).

                    except Exception as e:
                        # Checkpoint not advanced, so the blob is read again next run.
                        logger.error(
                            "BlobLogProcessor: Error processing blob %s/%s: %s",
                            cname, blob.name, str(e),
                        )
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
        "dropped=%d, retrying=%d, blobs_advanced=%d, blobs_seen=%d, blobs_with_unread=%d, "
        "batches=%d, bytes=%d, duration=%.1fs%s",
        total_stats["processed"],
        total_stats["uploaded"],
        total_stats["general"],
        total_stats["dropped"],
        total_stats["retrying"],
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
        if total_stats["uploaded"] > 0 and total_stats["dropped"] == 0 and total_stats["retrying"] == 0:
            log_event("info", "BlobLogProcessor",
                      f"Processed {total_stats['uploaded']} records in "
                      f"{total_stats['batches_sent']} batches "
                      f"({total_stats['duration_s']}s)",
                      {"stats": dict(total_stats)})
        if total_stats["dropped"] > 0:
            log_event("warning", "BlobLogProcessor",
                      f"Dropped {total_stats['dropped']} records",
                      {"stats": dict(total_stats)})
        if total_stats["retrying"] > 0:
            log_event("warning", "BlobLogProcessor",
                      f"{total_stats['retrying']} records not sent this run; "
                      "they are sent again on the next run",
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


def _read_append_records(blob_client, offset, length, with_boundaries=False):
    """Read complete JSON records only; leave an incomplete tail for the next poll.

    Returns ``(data, records, consumed_bytes, skipped_lines)``. Azure writes
    newline-delimited JSON, so an unparseable record that is already terminated
    by a newline is malformed, not incomplete: it is skipped and counted rather
    than blocking every record after it. Invalid UTF-8 bytes are carried as
    surrogate escapes so byte offsets stay exact.

    ``with_boundaries`` adds a fifth item: ``(records_so_far, bytes_consumed)``
    after each complete envelope, so a caller can checkpoint exactly past the
    records it managed to send.
    """
    if length <= 0:
        return (b"", [], 0, 0, []) if with_boundaries else (b"", [], 0, 0)
    data = blob_client.download_blob(offset=offset, length=length).readall()
    text = codecs.getincrementaldecoder("utf-8")("surrogateescape").decode(data, final=False)
    decoder = json.JSONDecoder()
    records = []
    boundaries = []
    skipped = 0
    cursor = 0
    while cursor < len(text):
        while cursor < len(text) and (text[cursor].isspace() or text[cursor] == ","):
            cursor += 1
        if cursor == len(text):
            break
        try:
            payload, end = decoder.raw_decode(text, cursor)
        except json.JSONDecodeError as error:
            newline = text.find("\n", cursor)
            if (newline == -1 or error.pos > newline
                    or error.msg.startswith("Unterminated string")):
                # A formatted envelope may contain newlines before the read
                # limit cuts it. Only discard a malformed first complete line;
                # do not consume a valid multiline prefix as corrupt data.
                break
            logger.warning("BlobLogProcessor: Skipping malformed append record at byte %d",
                           offset + _utf8_len(text[:cursor]))
            skipped += 1
            cursor = newline + 1
            continue
        if isinstance(payload, dict):
            batch = payload.get("records", [payload])
            if isinstance(batch, list):
                records.extend(batch)
        elif isinstance(payload, list):
            records.extend(payload)
        cursor = end
        if with_boundaries:
            boundaries.append((len(records), _utf8_len(text[:cursor])))
    if with_boundaries:
        return data, records, _utf8_len(text[:cursor]), skipped, boundaries
    return data, records, _utf8_len(text[:cursor]), skipped


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", "surrogateescape"))


def _block_boundaries(data, committed, first, upper, records):
    """``(records_before, block_after)`` for every block in ``committed[first:upper]``,
    or None when the blocks cannot be split safely.

    Azure commits whole records per block, so each block normally parses on its own.
    The split is trusted only when the blocks cover exactly the bytes read and their
    records, block by block, are the very records of the whole range in order; a
    matching count alone could pair the halves of a record split across blocks."""
    sizes = [committed[index].size for index in range(first, upper)]
    if sum(sizes) != len(data):
        return None
    boundaries, parsed, pos = [], [], 0
    for index, size in zip(range(first, upper), sizes):
        parsed.extend(_parse_records(data[pos:pos + size]))
        pos += size
        boundaries.append((len(parsed), index + 1))
    return boundaries if parsed == records else None


def _resume_skip(cp_entry, field, position, available) -> int:
    """Records already handled at ``position`` by an earlier run (0 if unknown)."""
    skip = cp_entry.get("skip_records", 0)
    if (cp_entry.get(field) != position or not isinstance(skip, int) or isinstance(skip, bool)
            or not 0 <= skip <= available):
        return 0
    return skip


def _batches(records):
    """Consecutive batches of at most MAX_RECORDS_PER_BATCH records and about
    MAX_BATCH_BYTES of JSON each (a single larger record goes alone)."""
    batch, size = [], 0
    for record in records:
        record_size = len(json.dumps(record, default=str))
        if batch and (len(batch) >= MAX_RECORDS_PER_BATCH or size + record_size > MAX_BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(record)
        size += record_size
    if batch:
        yield batch


def _upload_in_order(client, source_config_b64, records, using_general,
                     stats, acct_name, container, blob_name) -> int:
    """Upload ``records`` in order and return how many leading records were handled.

    Stops at the first batch that failed for a reason a retry can fix (Site24x7
    unreachable, Device Key refused), or when the run's deadline has passed:
    later batches are not sent ahead of it, so a caller that checkpoints past
    exactly the handled records re-sends nothing that already arrived. The rest
    count as retrying. A batch Site24x7 refused for good is dropped (a retry
    cannot fix it) and the upload goes on; a payload too large is split first.
    Records that could not be parsed are counted as dropped too."""
    sent = 0
    queue = list(_batches(records))
    while queue:
        batch = queue.pop(0)
        if time.monotonic() > stats.get("deadline", float("inf")):
            stats["time_budget_exhausted"] = True
            break
        success = client.post_logs(source_config_b64, batch)
        stats["batches_sent"] += 1
        if getattr(client, "upload_refused", False) is True:
            stats["upload_refused"] = True
        unparsed = getattr(client, "last_unparsed", 0) if success else 0
        unparsed = unparsed if isinstance(unparsed, int) else 0
        if not success:
            rejected = getattr(client, "last_rejected", 0)
            rejected = rejected if isinstance(rejected, int) and not isinstance(rejected, bool) else 0
            if rejected == 413 and len(batch) > 1:
                half = len(batch) // 2
                queue[0:0] = [batch[:half], batch[half:]]
                continue
            if rejected:
                stats["dropped"] += len(batch)
                stats["rejected"] = stats.get("rejected", 0) + len(batch)
                stats["error_blobs"].append({
                    "account": acct_name, "container": container, "blob": blob_name,
                    "error": f"Site24x7 refused {len(batch)} record(s) (HTTP {rejected})",
                })
                sent += len(batch)
                continue
            stats["error_blobs"].append({
                "account": acct_name, "container": container,
                "blob": blob_name, "error": "post_logs failed",
            })
            break
        stats["uploaded"] += len(batch) - unparsed
        stats["dropped"] += unparsed
        if using_general:
            stats["general"] += len(batch) - unparsed
        sent += len(batch)
    # Not sent this run; the checkpoint keeps them for the next one.
    stats["retrying"] += len(records) - sent
    return sent


def _upload_records(client, source_config_b64, records, using_general,
                     stats, acct_name, container, blob_name) -> bool:
    """Upload all of ``records``; True only if every one was sent."""
    return _upload_in_order(client, source_config_b64, records, using_general,
                            stats, acct_name, container, blob_name) == len(records)


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
        # GC again after the merge: the remote copy still holds entries the local
        # GC dropped, and merging would otherwise bring them back for good.
        merged = _gc_checkpoints(_merge_checkpoints(existing, local))

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
                    merged = _gc_checkpoints(_merge_checkpoints(existing, local))
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
        logger.error(
            "BlobLogProcessor: Checkpoint save gave up after retries "
            "(concurrent contention)"
        )
    except Exception as e:
        logger.error("BlobLogProcessor: Failed to save checkpoints: %s", str(e))


def _progress(entry: dict):
    """How far a checkpoint entry got, comparable only within its kind.

    Append blobs: (generation, next byte, records handled in the next envelope).
    Single-shot blobs: (version, done, records sent of that version).
    Block blobs: (next block, records handled in that block)."""
    def number(value):
        return value if isinstance(value, int) and not isinstance(value, bool) else 0
    if "next_byte" in entry:
        return ("append", str(entry.get("append_created", "")), number(entry.get("next_byte")),
                number(entry.get("skip_records")))
    if "partial_modified" in entry:
        return ("full", str(entry.get("partial_modified", "")), 0, number(entry.get("sent_records")))
    if entry.get("next_block") == -1:
        return ("full", str(entry.get("last_modified", "")), 1, 0)
    return ("block", number(entry.get("next_block")), number(entry.get("skip_records")))


def _merge_checkpoints(remote, local) -> dict:
    """Merge two per-blob checkpoint dicts.

    Each value is a dict ``{"next_block": int, "last_seen": iso, ...}``. On conflict
    the entry that got further wins **as a whole** (see ``_progress``), so a more
    advanced run never regresses and its resume fields stay together. When the kinds
    differ (a blob seen as full-read by one run and as append by another), progress
    is not comparable and the entry a run processed most recently (latest
    ``last_seen``) wins: a stale entry the local run only carried along cannot
    replace a newer one. ``last_seen`` is the latest of both (so GC works correctly).

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
            old, new = _progress(prev), _progress(v)
            if old[0] == new[0]:
                winner = prev if old > new else v
            else:
                winner = prev if str(prev.get("last_seen", "")) > str(v.get("last_seen", "")) else v
            out = dict(winner)
            out["last_seen"] = max(str(prev.get("last_seen", "")), str(v.get("last_seen", "")))
            merged[k] = out
    return merged
