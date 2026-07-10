import os
import json
import logging
from datetime import datetime, timezone

import azure.functions as func


_PHASE_NAMES = {
    1: "Fetching supported log types from Site24x7",
    2: "Discovering Azure resources",
    3: "Provisioning regional storage accounts",
    4: "Mapping diagnostic categories to resources",
    5: "Creating log types in Site24x7",
    6: "Configuring diagnostic settings",
}


def _update_phase(phase_num, progress=None, extra=None):
    """Merge the current phase into scan state so the dashboard can show progress.

    Uses ``update_scan_state`` (a concurrency-safe RMW merge), NOT
    ``save_scan_state`` (a full overwrite).  A full overwrite here would wipe
    ``last_scan_time``, the resource counts, ``s247_reachable`` and
    ``scan_started_at`` from the blob for the duration of the scan — exactly
    the fields the dashboard reads while a scan is running.
    """
    from shared.config_store import update_scan_state
    patch = {
        "in_progress": True,
        "current_phase": phase_num,
        "current_phase_name": _PHASE_NAMES.get(phase_num, f"Phase {phase_num}"),
    }
    if progress is not None:
        patch["phase_progress"] = progress
    if extra:
        patch.update(extra)
    try:
        update_scan_state(patch)
    except Exception:
        pass  # never let a progress update abort the scan


def _save_early_scan_state(save_scan_state, all_resources, active_resources, ignored_count):
    """Save a preliminary scan state so the Dashboard updates even if the
    full scan times out.  The final save at the end overwrites this."""
    scan_time = datetime.now(timezone.utc).isoformat()
    save_scan_state({
        "last_scan_time": scan_time,
        "total_resources": len(all_resources),
        "active_resources": len(active_resources),
        "ignored_resources": ignored_count,
        "newly_configured": 0,
        "updated": 0,
        "already_configured": 0,
        "removed": 0,
        "errors": 0,
        "s247_reachable": None,
        "in_progress": True,
        "current_phase": 3,
        "current_phase_name": _PHASE_NAMES[3],
    })


def run_scan():
    from shared.azure_manager import AzureManager
    from shared.region_manager import RegionManager
    from shared.ignore_list import load_ignore_list, is_ignored
    from shared.site24x7_client import Site24x7Client
    from shared.config_store import (
        get_supported_log_types,
        save_supported_log_types,
        get_logtype_config,
        save_logtype_config,
        get_all_logtype_configs,
        is_log_type_disabled,
        get_configured_resources,
        mark_resource_configured,
        unmark_resource_configured,
        save_configured_resources,
        save_scan_state,
        save_category_resource_types,
        clear_cache,
    )

    """Core scan logic: discover resources, create log types in Site24x7,
    store configs, and configure/reconcile diagnostic settings."""
    import time as _time
    _scan_start_mono = _time.monotonic()
    MAX_SCAN_SECONDS = 480  # 8 min hard guard (Consumption plan has 10 min max)

    def _elapsed():
        return _time.monotonic() - _scan_start_mono

    def _time_ok():
        return _elapsed() < MAX_SCAN_SECONDS

    from shared.debug_logger import log_event as _log_event
    logging.info("DiagSettingsManager: Starting periodic scan")
    _log_event("info", "DiagSettingsManager", "Scan phases starting")
    clear_cache()
    phase_timings = {}  # phase_name -> seconds

    # Load config
    subscription_ids = [
        s.strip()
        for s in os.environ.get("SUBSCRIPTION_IDS", "").split(",")
        if s.strip()
    ]
    general_enabled = os.environ.get("GENERAL_LOGTYPE_ENABLED", "false").lower() == "true"
    general_config_available = general_enabled and bool(
        os.environ.get("S247_GENERAL_LOGTYPE", "")
    )
    if general_enabled and not general_config_available:
        logging.warning(
            "DiagSettingsManager: GENERAL_LOGTYPE_ENABLED=true but S247_GENERAL_LOGTYPE "
            "is not set — general fallback will be skipped for resources without "
            "a specific log type config"
        )
    resource_group = os.environ.get("RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg"))
    diag_storage_suffix = os.environ.get("DIAG_STORAGE_SUFFIX", "")

    if not subscription_ids:
        logging.error("DiagSettingsManager: No SUBSCRIPTION_IDS configured")
        return {"error": "No SUBSCRIPTION_IDS configured"}

    azure_mgr = AzureManager()
    region_mgr = RegionManager(subscription_ids[0])
    s247_client = Site24x7Client()

    # ── Phase 1: Get supported log types ──
    _update_phase(1)
    phase_start = _time.monotonic()
    supported_types = get_supported_log_types()
    if not supported_types:
        logging.info("DiagSettingsManager: Fetching supported log types from Site24x7")
        result = s247_client.get_supported_log_types()
        if result and "supported_types" in result:
            # Build lookup map: normalized_name -> type_info
            # Two-pass to handle sub-categories correctly:
            # Pass 1: Index every entry by its logtype
            # Pass 2: Index sub-categories, preferring entries where
            #   logtype != sub-category (i.e., the "parent" logtype)
            types_map = {}
            for t in result["supported_types"]:
                logtype = t.get("logtype", "")
                types_map[logtype] = t

            for t in result["supported_types"]:
                logtype = t.get("logtype", "")
                for cat in t.get("log_categories", []):
                    cat_normalized = cat.replace("-", "").replace("_", "").lower()
                    existing = types_map.get(cat_normalized)
                    if not existing:
                        # No entry yet — set it
                        types_map[cat_normalized] = t
                    elif existing.get("logtype") == cat_normalized and logtype != cat_normalized:
                        # Existing entry is self-referencing (logtype == category name)
                        # but this entry is a true parent — prefer the parent
                        types_map[cat_normalized] = t

            save_supported_log_types(types_map)
            supported_types = types_map
            logging.info("DiagSettingsManager: Cached %d supported log types", len(types_map))
    logging.info("DiagSettingsManager: Phase 1 (supported types) done in %.1fs [total=%.1fs]",
                 _time.monotonic() - phase_start, _elapsed())
    phase_timings["phase1_supported_types"] = round(_time.monotonic() - phase_start, 1)
    _log_event("info", "DiagSettingsManager",
               f"Phase 1 done: {len(supported_types)} types in {_time.monotonic()-phase_start:.1f}s")

    # Load ignore list
    ignore_list = load_ignore_list()
    configured_resources = get_configured_resources()

    # ── Phase 2: Discover resources ──
    _update_phase(2)
    phase_start = _time.monotonic()
    all_resources = azure_mgr.get_all_resources(subscription_ids)
    logging.info("DiagSettingsManager: Phase 2 (discovery) — %d resources in %.1fs [total=%.1fs]",
                 len(all_resources), _time.monotonic() - phase_start, _elapsed())
    phase_timings["phase2_discovery"] = round(_time.monotonic() - phase_start, 1)
    _log_event("info", "DiagSettingsManager",
               f"Phase 2 done: {len(all_resources)} resources in {_time.monotonic()-phase_start:.1f}s")

    # Filter out ignored resources
    active_resources = [r for r in all_resources if not is_ignored(r, ignore_list)]
    ignored_count = len(all_resources) - len(active_resources)

    # Optionally exclude pipeline's own resources (prevents self-referential log loop)
    monitor_pipeline = os.environ.get("MONITOR_PIPELINE_RESOURCES", "false").lower() == "true"
    if not monitor_pipeline:
        pipeline_rg = resource_group.lower()
        before = len(active_resources)
        active_resources = [
            r for r in active_resources
            if r.get("resource_group", "").lower() != pipeline_rg
        ]
        pipeline_skipped = before - len(active_resources)
        if pipeline_skipped:
            ignored_count += pipeline_skipped
            logging.info(
                "DiagSettingsManager: Excluded %d pipeline resources in RG '%s' "
                "(MONITOR_PIPELINE_RESOURCES=false)",
                pipeline_skipped, resource_group,
            )
    logging.info(
        "DiagSettingsManager: %d active resources (%d ignored)",
        len(active_resources),
        ignored_count,
    )

    # Save early scan state so "Last Scan" updates even if the full scan times out
    _save_early_scan_state(save_scan_state, all_resources, active_resources, ignored_count)

    # Build a set of active resource IDs for cleanup
    active_resource_ids = {r.get("id", "") for r in active_resources}
    all_resource_ids = {r.get("id", "") for r in all_resources}

    # ── Phase 3: Region reconciliation ──
    _update_phase(3)
    phase_start = _time.monotonic()
    active_regions = region_mgr.get_active_regions(active_resources)
    provisioned_regions = region_mgr.get_provisioned_regions(resource_group)
    reconcile_result = region_mgr.reconcile_regions(
        resource_group, active_regions, provisioned_regions, diag_storage_suffix
    )
    provisioned_regions = region_mgr.get_provisioned_regions(resource_group)
    logging.info(
        "DiagSettingsManager: Phase 3 (regions) — added=%d, removed=%d in %.1fs [total=%.1fs]",
        len(reconcile_result.get("added", [])),
        len(reconcile_result.get("removed", [])),
        _time.monotonic() - phase_start, _elapsed(),
    )
    phase_timings["phase3_regions"] = round(_time.monotonic() - phase_start, 1)
    _log_event("info", "DiagSettingsManager",
               f"Phase 3 done: regions in {_time.monotonic()-phase_start:.1f}s [total={_elapsed():.0f}s]")

    # ── Phase 4: Category discovery ──
    _update_phase(4)
    phase_start = _time.monotonic()

    # Process each active resource
    stats = {
        "configured": 0, "updated": 0, "already_configured": 0,
        "removed": 0, "specific": 0, "general": 0, "skipped": 0,
        "logtypes_created": 0, "errors": 0,
    }

    # Collect categories that need log types created (batch for efficiency)
    categories_to_create = set()
    resource_category_map = {}

    # ── Phase 4a: Group resources by type, skip already-configured ──
    _category_cache_by_type = {}  # resource_type -> [categories] or None
    types_needing_discovery = {}  # resource_type -> sample_resource_id

    for resource in active_resources:
        resource_id = resource.get("id", "")
        location = resource.get("location", "")
        resource_type = resource.get("type", "")

        prev_config = configured_resources.get(resource_id, {})
        if prev_config and prev_config.get("categories"):
            sa_name = provisioned_regions.get(location, "")
            if sa_name:
                resource_category_map[resource_id] = {
                    "categories": prev_config["categories"],
                    "effective_categories": prev_config["categories"],
                    "location": location,
                    "sa_name": sa_name,
                }
                stats["already_configured"] += 1
                continue

        if resource_type not in types_needing_discovery:
            types_needing_discovery[resource_type] = resource_id

    _log_event("info", "DiagSettingsManager",
               f"Phase 4a: {len(types_needing_discovery)} unique types to discover, "
               f"{stats['already_configured']} already configured [{_elapsed():.0f}s]")

    # ── Phase 4b: Parallel category discovery with timeouts ──
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _discover_type(resource_type, sample_id):
        """Discover diagnostic categories for a resource type."""
        try:
            categories = azure_mgr.get_diagnostic_categories(sample_id)
            return resource_type, categories or []
        except Exception as e:
            logging.error("DiagSettingsManager: Category discovery failed for %s: %s",
                          resource_type, e)
            return resource_type, []

    with ThreadPoolExecutor(max_workers=10) as cat_executor:
        futures = {
            cat_executor.submit(_discover_type, rtype, sample_id): rtype
            for rtype, sample_id in types_needing_discovery.items()
        }
        cat_deadline = _scan_start_mono + 240  # 4 min hard cap for category discovery
        try:
            for future in as_completed(futures, timeout=max(1, cat_deadline - _time.monotonic())):
                try:
                    rtype, categories = future.result(timeout=30)
                    _category_cache_by_type[rtype] = categories
                    if categories:
                        logging.info("DiagSettingsManager: Cached %d categories for %s",
                                     len(categories), rtype)
                except Exception as e:
                    rtype = futures[future]
                    logging.error("DiagSettingsManager: Timeout/error for %s: %s", rtype, e)
                    _category_cache_by_type[rtype] = []
        except TimeoutError:
            # Overall discovery budget exhausted. Cancel stragglers and proceed
            # with whatever we have — the fallback loop below marks missing
            # types as empty so the scan degrades gracefully instead of failing.
            unfinished = [futures[f] for f in futures if not f.done()]
            logging.warning(
                "DiagSettingsManager: Category discovery budget exhausted; %d unfinished: %s",
                len(unfinished), unfinished,
            )
            for f in futures:
                if not f.done():
                    f.cancel()

    # Mark types that weren't resolved (timed out entirely) as empty
    for rtype in types_needing_discovery:
        if rtype not in _category_cache_by_type:
            _category_cache_by_type[rtype] = []
            logging.warning("DiagSettingsManager: Type %s not resolved in time", rtype)

    _log_event("info", "DiagSettingsManager",
               f"Phase 4b: {len(_category_cache_by_type)} types discovered in {_time.monotonic()-phase_start:.1f}s [{_elapsed():.0f}s]")

    # Persist category → resource_types mapping from ALL discovered resources
    # (used by dashboard to show which Azure resource types produce each category)
    _cat_rt_map = {}
    for rtype, cats in _category_cache_by_type.items():
        for cat in cats:
            cat_norm = cat.replace("-", "").replace("_", "").lower()
            _cat_rt_map.setdefault(cat_norm, set()).add(rtype)
    # Also include categories from already-configured resources
    for res_id, info in configured_resources.items():
        parts = res_id.split("/providers/")
        if len(parts) >= 2:
            segs = parts[-1].split("/")
            rtype = f"{segs[0]}/{segs[1]}" if len(segs) >= 2 else segs[0]
            for cat in info.get("categories", []):
                cat_norm = cat.replace("-", "").replace("_", "").lower()
                _cat_rt_map.setdefault(cat_norm, set()).add(rtype)
    # Deduplicate resource types by case (keep longest variant per lowercase key)
    for cat_norm in _cat_rt_map:
        by_lower = {}
        for rt in _cat_rt_map[cat_norm]:
            key = rt.lower()
            if key not in by_lower or len(rt) > len(by_lower[key]):
                by_lower[key] = rt
        _cat_rt_map[cat_norm] = set(by_lower.values())
    # Convert sets to sorted lists and save
    save_category_resource_types({k: sorted(v) for k, v in _cat_rt_map.items()})

    # ── Phase 4c: Map categories to resources ──
    for resource in active_resources:
        resource_id = resource.get("id", "")
        if resource_id in resource_category_map:
            continue  # already handled (configured or duplicate)
        location = resource.get("location", "")
        resource_type = resource.get("type", "")

        try:
            categories = _category_cache_by_type.get(resource_type, [])
            if not categories:
                stats["skipped"] += 1
                continue

            sa_name = provisioned_regions.get(location, "")
            if not sa_name:
                logging.warning(
                    "DiagSettingsManager: No storage account for region %s, skipping %s",
                    location, resource_id,
                )
                stats["skipped"] += 1
                continue

            desired_categories = [
                c for c in categories if not is_log_type_disabled(c)
            ]

            effective_categories = []
            for category in desired_categories:
                cat_normalized = category.replace("-", "").replace("_", "").replace(" ", "").lower()
                if get_logtype_config(cat_normalized):
                    effective_categories.append(category)
                    stats["specific"] += 1
                elif general_config_available:
                    effective_categories.append(category)
                    stats["general"] += 1

            resource_category_map[resource_id] = {
                "categories": desired_categories,
                "effective_categories": effective_categories,
                "location": location,
                "sa_name": sa_name,
            }

            for category in desired_categories:
                cat_normalized = category.replace("-", "").replace("_", "").replace(" ", "").lower()
                existing_config = get_logtype_config(cat_normalized)
                if not existing_config:
                    if supported_types and cat_normalized in supported_types:
                        parent_logtype = supported_types[cat_normalized].get("logtype", category)
                        categories_to_create.add(parent_logtype)

        except Exception as e:
            logging.error(
                "DiagSettingsManager: Error mapping resource %s: %s",
                resource_id, str(e),
            )
            stats["errors"] += 1

    logging.info(
        "DiagSettingsManager: Phase 4 (category discovery) done — %d resources mapped, "
        "%d unique resource types cached, %d categories to create in %.1fs [total=%.1fs]",
        len(resource_category_map), len(_category_cache_by_type),
        len(categories_to_create), _time.monotonic() - phase_start, _elapsed(),
    )
    phase_timings["phase4_categories"] = round(_time.monotonic() - phase_start, 1)
    _log_event("info", "DiagSettingsManager",
               f"Phase 4 done: {len(resource_category_map)} mapped, {len(categories_to_create)} to create in {_time.monotonic()-phase_start:.1f}s [total={_elapsed():.0f}s]")

    # ── Phase 4d: record the Entra ID (tenant-log) target storage account ──
    # Entra logs are provisioned per-category on demand from the dashboard's
    # Entra tab (UpdateEntraLogTypes), and the tenant admin points the Entra
    # diagnostic setting at a storage account manually. We only need to publish
    # WHICH storage account they should target — the first regional SA. Recorded
    # unconditionally (cheap) so the guide always has a concrete target to show.
    entra_target_sa = region_mgr.get_primary_storage_account(resource_group)

    # ── Phase 5: Create log types ──
    _update_phase(5, progress=f"{len(categories_to_create)} log types")
    phase_start = _time.monotonic()

    # Pre-flight check: validate S247 connectivity before attempting bulk creation.
    # This catches relay outages, auth issues, and network problems up front
    # instead of wasting time on per-category calls that will all fail.
    s247_reachable = True
    s247_errors = []  # structured error list for dashboard

    if categories_to_create:
        preflight = s247_client.preflight_check()
        _log_event("info", "DiagSettingsManager",
                   f"Phase 5 preflight: ok={preflight['ok']} latency={preflight.get('latency_ms',0)}ms "
                   f"error={preflight.get('error','none')}")
        if not preflight["ok"]:
            s247_reachable = False
            s247_errors.append({
                "phase": "preflight",
                "message": f"S247 connectivity check failed: {preflight.get('error','')}",
                "latency_ms": preflight.get("latency_ms"),
            })
            _log_event("error", "DiagSettingsManager",
                       f"Phase 5: preflight FAILED — skipping log type creation. "
                       f"Error: {preflight.get('error','')}")
            logging.error(
                "DiagSettingsManager: S247 preflight failed (%dms) — %s. "
                "Skipping log type creation for %d categories.",
                preflight.get("latency_ms", 0), preflight.get("error", ""),
                len(categories_to_create),
            )
        else:
            _log_event("info", "DiagSettingsManager",
                       f"Phase 5: creating {len(categories_to_create)} log types: "
                       f"{list(categories_to_create)[:10]}")
            logging.info(
                "DiagSettingsManager: Creating %d log types in Site24x7: %s",
                len(categories_to_create), list(categories_to_create)[:10],
            )
            created = s247_client.create_log_types(
                list(categories_to_create),
                supported_types=supported_types,
            )
            _log_event("info", "DiagSettingsManager",
                       f"Phase 5: create_log_types returned "
                       f"{len(created) if created else 0} results")
            if created:
                # Collect any per-category errors from the batch
                batch_errors = created[0].pop("_errors", []) if created else []
                for err in batch_errors:
                    s247_errors.append({
                        "phase": "logtype_creation",
                        "category": err.get("category"),
                        "message": err.get("message"),
                    })

                for lt in created:
                    category_key = lt.get("category", "")
                    source_config = lt.get("sourceConfig")
                    if category_key and source_config:
                        try:
                            cat_name = category_key.replace("S247_", "")
                            save_logtype_config(cat_name, source_config)
                            stats["logtypes_created"] += 1

                            # Also save for all log_categories sub-categories
                            if supported_types and cat_name in supported_types:
                                for sub_cat in supported_types[cat_name].get("log_categories", []):
                                    sub_normalized = sub_cat.replace("-", "").replace("_", "").lower()
                                    if sub_normalized != cat_name:
                                        save_logtype_config(sub_normalized, source_config)
                                        logging.info(
                                            "DiagSettingsManager: Saved config for sub-category '%s' "
                                            "(parent: %s)", sub_normalized, cat_name,
                                        )
                        except Exception as e:
                            logging.error(
                                "DiagSettingsManager: Failed to save config for %s: %s",
                                category_key, str(e),
                            )
            else:
                # Preflight already verified connectivity — if every category
                # in this batch failed, it's almost certainly because the user
                # deleted those log types in S247 (LOG_TYPE_NOT_FOUND). Do NOT
                # flip s247_reachable, so Phase 5b (refresh) and Phase 6 (diag
                # settings) still run for the rest of the catalog.
                s247_errors.append({
                    "phase": "logtype_creation",
                    "message": (
                        f"All {len(categories_to_create)} log type creation(s) failed — "
                        f"likely soft-deleted on Site24x7 or not recognized. "
                        f"Categories: {list(categories_to_create)[:5]}"
                    ),
                })
                logging.warning(
                    "DiagSettingsManager: All %d log type creation(s) failed (server "
                    "reachable — likely soft-deleted entries). Continuing with refresh "
                    "and diagnostic-setting phases using existing cached configs.",
                    len(categories_to_create),
                )

    logging.info(
        "DiagSettingsManager: Phase 5 (logtype creation) done in %.1fs [total=%.1fs]",
        _time.monotonic() - phase_start, _elapsed(),
    )
    phase_timings["phase5_logtype_creation"] = round(_time.monotonic() - phase_start, 1)
    _log_event("info", "DiagSettingsManager",
               f"[total={_elapsed():.0f}s] reachable={s247_reachable} errors={len(s247_errors)}")

    # ── Phase 5b: Refresh existing logtype configs ──
    # Re-fetch sourceConfig from S247 for all existing configs and update if changed.
    # This catches log type updates (field changes, new jsonPath, etc.) made on S247.
    configs_refreshed = 0
    if s247_reachable and _time_ok():
        phase5b_start = _time.monotonic()
        try:
            existing_configs = get_all_logtype_configs()
            if existing_configs:
                _log_event("info", "DiagSettingsManager",
                           f"Phase 5b: refreshing {len(existing_configs)} existing logtype configs")

                from concurrent.futures import ThreadPoolExecutor, as_completed

                # Fields we compare (skip apiKey/uploadDomain — those are ours)
                _COMPARE_FIELDS = ("logType", "jsonPath", "dateField", "dateFormat",
                                   "filterConfig", "maskingConfig", "hashingConfig", "derivedConfig")

                def _fallback_for(cat_name):
                    # Same mapping used in Phase 5: sub-categories (e.g. joblogs,
                    # jobstreams) don't exist as standalone log types on S247 —
                    # their parent (AutomationRunbookJobs) does. Without this
                    # fallback, the refresh call returns "Log Type Not Found"
                    # and pollutes the error log on every scan.
                    if not supported_types:
                        return None
                    normalized = cat_name.replace("-", "").replace("_", "").replace(" ", "").lower()
                    info = supported_types.get(normalized, {})
                    display = info.get("display_name", "")
                    return [display] if display else None

                def _refresh_one(config_key, stored_config):
                    cat_name = config_key.replace("S247_", "")
                    fresh = s247_client.create_log_type(cat_name, fallback_names=_fallback_for(cat_name))
                    if not fresh:
                        return None
                    # Compare relevant fields
                    changed = False
                    for field in _COMPARE_FIELDS:
                        if stored_config.get(field) != fresh.get(field):
                            changed = True
                            break
                    if changed:
                        return (cat_name, fresh)
                    return None

                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures = {
                        executor.submit(_refresh_one, k, v): k
                        for k, v in existing_configs.items()
                    }
                    for future in as_completed(futures):
                        if not _time_ok():
                            break
                        try:
                            result = future.result()
                            if result:
                                cat_name, fresh_config = result
                                save_logtype_config(cat_name, fresh_config)
                                configs_refreshed += 1
                                logging.info(
                                    "DiagSettingsManager: Config refreshed for '%s'",
                                    cat_name,
                                )
                        except Exception as e:
                            logging.warning(
                                "DiagSettingsManager: Config refresh failed for %s: %s",
                                futures[future], e,
                            )

                phase5b_elapsed = round(_time.monotonic() - phase5b_start, 1)
                phase_timings["phase5b_config_refresh"] = phase5b_elapsed
                _log_event("info", "DiagSettingsManager",
                           f"Phase 5b done: {configs_refreshed}/{len(existing_configs)} "
                           f"configs refreshed in {phase5b_elapsed}s [total={_elapsed():.0f}s]")
        except Exception as e:
            _log_event("warning", "DiagSettingsManager",
                       f"Phase 5b config refresh failed: {e}")
            logging.warning("DiagSettingsManager: Config refresh error: %s", e)

    # Re-evaluate effective_categories after log type creation
    _log_event("info", "DiagSettingsManager",
               f"Re-evaluating categories for {len(resource_category_map)} resources [total={_elapsed():.0f}s]")
    try:
        for resource_id, info in resource_category_map.items():
            effective = []
            for category in info["categories"]:
                cat_normalized = category.replace("-", "").replace("_", "").replace(" ", "").lower()
                if get_logtype_config(cat_normalized) or general_config_available:
                    effective.append(category)
            info["effective_categories"] = effective
        _log_event("info", "DiagSettingsManager",
                   f"Re-evaluation done [total={_elapsed():.0f}s]")
    except Exception as e:
        _log_event("error", "DiagSettingsManager",
                   f"Re-evaluation FAILED: {e} [total={_elapsed():.0f}s]")
        raise

    # ── Phase 6: Configure diagnostic settings ──
    phase_start = _time.monotonic()
    _log_event("info", "DiagSettingsManager",
               f"Phase 6 entry: _time_ok={_time_ok()} elapsed={_elapsed():.0f}s budget={MAX_SCAN_SECONDS}s")

    if not _time_ok():
        logging.warning(
            "DiagSettingsManager: Time budget exhausted (%.0fs) before config phase — "
            "saving partial results",
            _elapsed(),
        )
        # Save what we have and exit
        scan_time = datetime.now(timezone.utc).isoformat()
        save_scan_state({
            "last_scan_time": scan_time,
            "total_resources": len(all_resources),
            "active_resources": len(active_resources),
            "ignored_resources": ignored_count,
            "newly_configured": 0, "updated": 0,
            "already_configured": stats["already_configured"],
            "removed": 0, "errors": stats["errors"],
            "s247_reachable": s247_reachable,
            "in_progress": False,
            "time_budget_exhausted": True,
        })
        return {"time_budget_exhausted": True, **stats}

    # Resources already fast-pathed above (already_configured) should be skipped
    fast_pathed_ids = {rid for rid, info in resource_category_map.items()
                       if rid in configured_resources
                       and set(info.get("effective_categories", [])) == set(configured_resources[rid].get("categories", []))}

    # Use ThreadPoolExecutor for parallel diagnostic settings creation
    from shared.azure_manager import _extract_subscription_id

    def _configure_one_resource(resource_id, info):
        """Configure diagnostic setting for a single resource.
        Returns (action, error, mark_data) where mark_data is
        (resource_id, categories, sa_name) if resource should be marked configured."""
        try:
            res_sub_id = _extract_subscription_id(resource_id) or subscription_ids[0]
            storage_account_id = (
                f"/subscriptions/{res_sub_id}/resourceGroups/{resource_group}"
                f"/providers/Microsoft.Storage/storageAccounts/{info['sa_name']}"
            )

            effective = info["effective_categories"]
            prev_config = configured_resources.get(resource_id, {})
            prev_categories = set(prev_config.get("categories", []))
            new_categories = set(effective)

            if not effective:
                if resource_id in configured_resources:
                    azure_mgr.delete_diagnostic_setting(resource_id)
                    return "removed", None, ("unmark", resource_id)
                return "skipped", None, None

            if resource_id in configured_resources:
                if new_categories == prev_categories:
                    return "already_configured", None, None
                success = azure_mgr.create_diagnostic_setting(
                    resource_id=resource_id,
                    storage_account_id=storage_account_id,
                    categories=effective,
                )
                if success:
                    return "updated", None, ("mark", resource_id, effective, info["sa_name"])
                return "error", "create_diagnostic_setting returned False", None
            else:
                success = azure_mgr.create_diagnostic_setting(
                    resource_id=resource_id,
                    storage_account_id=storage_account_id,
                    categories=effective,
                )
                if success:
                    return "configured", None, ("mark", resource_id, effective, info["sa_name"])
                return "error", "create_diagnostic_setting returned False", None
        except Exception as e:
            return "error", str(e), None

    resources_to_configure = {
        rid: info for rid, info in resource_category_map.items()
        if rid not in fast_pathed_ids
    }
    logging.info(
        "DiagSettingsManager: Configuring %d resources in parallel (%d fast-pathed)",
        len(resources_to_configure), len(fast_pathed_ids),
    )
    _log_event("info", "DiagSettingsManager",
               f"Phase 6 starting: {len(resources_to_configure)} to configure, {len(fast_pathed_ids)} fast-pathed [total={_elapsed():.0f}s]")
    _update_phase(6, progress=f"0/{len(resources_to_configure)} resources")

    config_start = _time.monotonic()
    pending_marks = []  # Collect (resource_id, categories, sa_name) for batch save
    config_deadline = _scan_start_mono + MAX_SCAN_SECONDS

    def _flush_marks():
        """Save pending marks to blob in one write."""
        nonlocal pending_marks
        if not pending_marks:
            return
        batch_configured = get_configured_resources()
        now_iso = datetime.now(timezone.utc).isoformat()
        for mark in pending_marks:
            if mark[0] == "mark":
                _, res_id, cats, sa_name = mark
                batch_configured[res_id] = {
                    "categories": cats,
                    "storage_account": sa_name,
                    "configured_at": now_iso,
                }
            elif mark[0] == "unmark":
                _, res_id = mark
                batch_configured.pop(res_id, None)
        save_configured_resources(batch_configured)
        logging.info("DiagSettingsManager: Flushed %d resource marks to blob", len(pending_marks))
        pending_marks = []

    from concurrent.futures import wait, FIRST_COMPLETED

    executor = ThreadPoolExecutor(max_workers=10)
    remaining_futures = {
        executor.submit(_configure_one_resource, rid, info): rid
        for rid, info in resources_to_configure.items()
    }
    done_count = 0
    timed_out = False

    while remaining_futures and _time.monotonic() < config_deadline:
        done, _ = wait(remaining_futures.keys(), timeout=15, return_when=FIRST_COMPLETED)
        for future in done:
            rid = remaining_futures.pop(future)
            try:
                action, error, mark_data = future.result(timeout=1)
            except Exception as e:
                action, error, mark_data = "error", f"Exception: {e}", None
            if action == "error":
                logging.error("DiagSettingsManager: Error configuring %s: %s", rid, error)
                stats["errors"] += 1
            elif action in stats:
                stats[action] += 1
            if mark_data:
                pending_marks.append(mark_data)
            done_count += 1
            if done_count % 25 == 0:
                elapsed = _time.monotonic() - config_start
                logging.info("DiagSettingsManager: Progress — %d/%d resources (%.1fs)",
                             done_count, len(resources_to_configure), elapsed)
                _log_event("info", "DiagSettingsManager",
                           f"Phase 6 progress: {done_count}/{len(resources_to_configure)} ({elapsed:.0f}s) "
                           f"err={stats.get('errors',0)}")
                _update_phase(6,
                              progress=f"{done_count}/{len(resources_to_configure)} resources")
                _flush_marks()

    if remaining_futures:
        timed_out = True
        logging.warning(
            "DiagSettingsManager: Time budget hit — %d/%d resources done, %d still running",
            done_count, len(resources_to_configure), len(remaining_futures),
        )
        # Cancel unstarted futures (can't cancel running ones)
        for f in remaining_futures:
            f.cancel()

    # Final flush of any remaining marks
    _flush_marks()

    config_elapsed = _time.monotonic() - config_start
    logging.info(
        "DiagSettingsManager: Parallel config complete — %d/%d done in %.1fs "
        "(configured=%d, updated=%d, errors=%d, skipped=%d)",
        done_count, len(resources_to_configure), config_elapsed,
        stats.get("configured", 0), stats.get("updated", 0),
        stats.get("errors", 0), stats.get("skipped", 0),
    )
    _log_event("info", "DiagSettingsManager",
               f"Phase 6 done: {done_count}/{len(resources_to_configure)} in {config_elapsed:.0f}s "
               f"(cfg={stats.get('configured',0)} upd={stats.get('updated',0)} "
               f"err={stats.get('errors',0)} skip={stats.get('skipped',0)} "
               f"timeout={timed_out}) [total={_elapsed():.0f}s]")
    phase_timings["phase6_diag_settings"] = round(config_elapsed, 1)

    # ── Cleanup: remove settings for ignored/deleted resources ──
    elapsed_total = _time.monotonic() - _scan_start_mono
    if elapsed_total > MAX_SCAN_SECONDS:
        logging.warning(
            "DiagSettingsManager: Skipping cleanup — already %.0fs elapsed (max %ds). "
            "Will clean up on next scan.",
            elapsed_total, MAX_SCAN_SECONDS,
        )
    else:
        cleanup_count = 0
        stale_resource_ids = []
        for res_id in list(configured_resources.keys()):
            if res_id not in active_resource_ids:
                try:
                    azure_mgr.delete_diagnostic_setting(res_id)
                    logging.info(
                        "DiagSettingsManager: Cleaned up diagnostic setting for "
                        "ignored/deleted resource %s", res_id,
                    )
                except Exception as e:
                    logging.warning(
                        "DiagSettingsManager: Failed to clean up diagnostic setting for %s: %s",
                        res_id, str(e),
                    )
                stale_resource_ids.append(res_id)
                cleanup_count += 1

        if stale_resource_ids:
            current_configured = get_configured_resources()
            for res_id in stale_resource_ids:
                current_configured.pop(res_id, None)
            save_configured_resources(current_configured)
            stats["removed"] += cleanup_count
            logging.info(
                "DiagSettingsManager: Cleaned up %d stale configured resource entries",
                cleanup_count,
            )

    # Update last scan time — save to blob (primary) and app setting (fallback)
    scan_time = datetime.now(timezone.utc).isoformat()
    scan_state = {
        "last_scan_time": scan_time,
        "total_resources": len(all_resources),
        "active_resources": len(active_resources),
        "ignored_resources": ignored_count,
        "newly_configured": stats["configured"],
        "updated": stats["updated"],
        "already_configured": stats["already_configured"],
        "removed": stats["removed"],
        "skipped": stats["skipped"],
        "logtypes_created": stats["logtypes_created"],
        "configs_refreshed": configs_refreshed,
        "errors": stats["errors"],
        "s247_reachable": s247_reachable,
        "s247_errors": s247_errors if s247_errors else [],
        "phase_timings": phase_timings,
        "total_duration": round(_elapsed(), 1),
        "regions_count": len(provisioned_regions),
        "unique_resource_types": len(_category_cache_by_type),
        "entra_target_storage_account_id": entra_target_sa.get("id", ""),
        "entra_target_storage_account_name": entra_target_sa.get("name", ""),
        "in_progress": False,
    }
    save_scan_state(scan_state)
    try:
        azure_mgr.update_app_setting("LAST_SCAN_TIME", scan_time)
    except Exception as e:
        logging.warning("DiagSettingsManager: Failed to update LAST_SCAN_TIME app setting: %s", str(e))

    summary = {
        "scan_time": scan_time,
        "total_resources": len(all_resources),
        "active_resources": len(active_resources),
        "ignored_resources": ignored_count,
        "already_configured": stats["already_configured"],
        "newly_configured": stats["configured"],
        "updated": stats["updated"],
        "removed": stats["removed"],
        "logtypes_created": stats["logtypes_created"],
        "specific_logtypes": stats["specific"],
        "general_logtypes": stats["general"],
        "skipped": stats["skipped"],
        "errors": stats["errors"],
        "regions": {
            "active": list(active_regions),
            "added": reconcile_result.get("added", []),
            "removed": reconcile_result.get("removed", []),
        },
    }

    logging.info(
        "DiagSettingsManager: Scan complete — configured=%d (new=%d, updated=%d, existing=%d, removed=%d), "
        "logtypes_created=%d, specific=%d, general=%d, skipped=%d, errors=%d",
        stats["configured"] + stats["updated"] + stats["already_configured"],
        stats["configured"],
        stats["updated"],
        stats["already_configured"],
        stats["removed"],
        stats["logtypes_created"],
        stats["specific"],
        stats["general"],
        stats["skipped"],
        stats["errors"],
    )

    # Persist scan summary for Debug API
    try:
        from shared.debug_logger import log_event
        log_event("info", "DiagSettingsManager", "Scan complete", summary)
        if stats["errors"] > 0:
            log_event("warning", "DiagSettingsManager",
                      f"Scan had {stats['errors']} errors",
                      {"stats": stats})
        if not s247_reachable:
            log_event("error", "DiagSettingsManager",
                      "Site24x7 was unreachable during scan",
                      {"categories_needing_creation": len(categories_to_create),
                       "errors": s247_errors})
    except Exception:
        pass

    return summary


def main(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("DiagSettingsManager: Timer is past due")
    auto_scan = os.environ.get("AUTO_SCAN_ENABLED", "true").lower() == "true"
    if not auto_scan:
        logging.info("DiagSettingsManager: Auto-scan is DISABLED — skipping scheduled scan")
        return

    # Concurrency guard: skip if another scan is already in progress.
    # A stale flag older than the TTL (see try_acquire_scan_lock) is treated
    # as a crashed scan and overwritten.
    try:
        from shared.config_store import try_acquire_scan_lock
        if not try_acquire_scan_lock():
            logging.info(
                "DiagSettingsManager: Another scan is in progress — skipping"
            )
            try:
                from shared.debug_logger import log_event
                log_event("info", "DiagSettingsManager",
                          "Scheduled scan skipped — concurrent scan in progress")
            except Exception:
                pass
            return
    except Exception as guard_err:
        # If the guard itself fails, fall through to the scan — the old
        # behaviour is preferable to silently skipping every scheduled run.
        logging.warning(
            "DiagSettingsManager: scan lock check failed (%s) — proceeding",
            guard_err,
        )

    run_scan()
