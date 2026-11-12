"""Log Collection switch, the paged resource list, one category spelling and the checkpoint guard."""
import json
from unittest.mock import MagicMock, patch

import azure.functions as func
import pytest

import GetConfiguredResources
import LogCollection
import ScanWorker
from shared import collection, config_store

SUB = "00000000-0000-0000-0000-000000000001"


def _req(method="PUT", body=None, params=None, route="collection"):
    return func.HttpRequest(method=method, url=f"/api/{route}", params=params or {},
                            body=json.dumps(body).encode() if body is not None else b"")


# ---------------------------------------------------------------- switch


@pytest.mark.parametrize("body", [{}, {"enabled": "false"}, {"enabled": 0}, [True]])
def test_switch_takes_a_json_boolean_only(body):
    out = MagicMock()
    assert LogCollection.main(_req(body=body), out).status_code == 400
    out.set.assert_not_called()


def test_switching_off_stops_forwarding_first_and_queues_the_removal():
    order, out = [], MagicMock()
    with patch("shared.config_store.save_processing_enabled", side_effect=lambda e: order.append(("processing", e)) or True), \
         patch("shared.config_store.update_collection_state", side_effect=lambda p: order.append(("state", p["enabled"])) or p), \
         patch("shared.debug_logger.log_audit"):
        resp = LogCollection.main(_req(body={"enabled": False}), out)
    assert resp.status_code == 202
    assert order == [("processing", False), ("state", False)]
    assert json.loads(out.set.call_args.args[0])["action"] == collection.STOP_ACTION


def test_switching_on_resumes_and_queues_a_scan():
    out = MagicMock()
    with patch("shared.config_store.save_processing_enabled", return_value=True) as processing, \
         patch("shared.config_store.update_collection_state", side_effect=lambda p: p), \
         patch("shared.debug_logger.log_audit"):
        assert LogCollection.main(_req(body={"enabled": True}), out).status_code == 202
    processing.assert_called_once_with(True)
    message = json.loads(out.set.call_args.args[0])
    assert message["source"] == collection.START_SOURCE and "action" not in message


def test_nothing_is_queued_when_the_state_cannot_be_saved():
    out = MagicMock()
    with patch("shared.config_store.save_processing_enabled", return_value=True), \
         patch("shared.config_store.update_collection_state", return_value=None):
        assert LogCollection.main(_req(body={"enabled": False}), out).status_code == 500
    out.set.assert_not_called()


def _msg(body, dequeue=1):
    msg = MagicMock()
    msg.get_body.return_value = json.dumps(body).encode()
    msg.dequeue_count = dequeue
    return msg


def test_worker_runs_the_removal_under_the_scan_lock():
    with patch("shared.config_store.try_acquire_scan_lock", return_value=True), \
         patch("shared.collection.stop_collection") as stop, \
         patch("ScanWorker._clear_in_progress") as clear, \
         patch("shared.registration.register_with_site24x7"), \
         patch("shared.debug_logger.log_event"):
        ScanWorker.main(_msg({"action": collection.STOP_ACTION, "requested_at": "2026-11-13T00:00:00+00:00"}))
    stop.assert_called_once()
    clear.assert_called_once()


def test_worker_retries_the_removal_while_a_scan_runs():
    with patch("shared.config_store.try_acquire_scan_lock", return_value=False), \
         patch("shared.collection.stop_collection") as stop, \
         patch("shared.debug_logger.log_event"):
        with pytest.raises(RuntimeError):
            ScanWorker.main(_msg({"action": collection.STOP_ACTION}))
    stop.assert_not_called()


def test_worker_drops_scans_while_collection_is_off():
    with patch("shared.config_store.collection_enabled", return_value=False), \
         patch("shared.config_store.try_acquire_scan_lock") as lock:
        ScanWorker.main(_msg({"source": "dashboard", "requested_at": "2026-11-13T00:00:00+00:00"}))
    lock.assert_not_called()


def test_scan_now_checks_every_tracked_setting():
    with patch("shared.config_store.collection_enabled", return_value=True), \
         patch("ScanWorker._already_scanned_since", return_value=False), \
         patch("shared.config_store.try_acquire_scan_lock", return_value=True), \
         patch("DiagSettingsManager.run_scan", return_value={}) as run, \
         patch("shared.registration.register_with_site24x7"), \
         patch("shared.debug_logger.log_event"):
        ScanWorker.main(_msg({"source": "dashboard", "requested_at": "2026-11-13T00:00:00+00:00"}))
    assert run.call_args.kwargs["full_reconcile"] is True


def test_a_given_up_removal_says_so_and_leaves_the_lock_alone():
    with patch("shared.collection.mark_stop_abandoned") as abandoned, \
         patch("ScanWorker._clear_in_progress") as clear, \
         patch("shared.debug_logger.log_event"):
        ScanWorker.main(_msg({"action": collection.STOP_ACTION}, dequeue=ScanWorker.MAX_ATTEMPTS + 1))
    abandoned.assert_called_once()
    clear.assert_not_called()


def test_stop_records_a_partial_removal():
    saved = []
    mgr = MagicMock()
    mgr.remove_all_diagnostic_settings.return_value = {"removed": 3, "skipped": 0, "errors": 1, "unresolved": 0,
                                                       "resolved_ids": ["a", "b", "c"], "details": []}
    with patch("shared.config_store.update_collection_state", side_effect=lambda p: saved.append(p) or p), \
         patch("shared.subscriptions.monitored_subscriptions", return_value=([SUB], {}, None)), \
         patch("shared.azure_manager.AzureManager", return_value=mgr), \
         patch("shared.config_store.get_configured_resources", return_value={}), \
         patch("shared.config_store.unmark_resource_configured", return_value=True), \
         patch("shared.collection._clear_all", return_value={"accounts": 2, "containers_cleared": 4, "storage_errors": 0}), \
         patch("shared.debug_logger.log_event"), patch("shared.debug_logger.log_audit"):
        outcome = collection.stop_collection()
    assert outcome["state"] == "partial" and outcome["removed"] == 3 and outcome["errors"] == 1
    assert saved[0]["stop"]["state"] == "running" and saved[-1]["stop"]["state"] == "partial"


def test_startup_scan_is_skipped_while_collection_is_off(monkeypatch):
    from shared import startup_scan
    monkeypatch.setattr(startup_scan, "_checked", False)
    with patch("shared.config_store.get_collection_state", return_value={"enabled": False}), \
         patch("shared.config_store.get_scan_state") as scan_state:
        assert startup_scan.startup_scan_reason() is None
    scan_state.assert_not_called()


# ---------------------------------------------------------------- resources list


def _configured(n_sites, n_vaults):
    out = {}
    for i in range(n_sites):
        out[f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Web/sites/app{i:02d}"] = {"categories": ["A"]}
    for i in range(n_vaults):
        out[f"/subscriptions/{SUB}/resourcegroups/rg/providers/microsoft.keyvault/vaults/kv{i}"] = {"categories": ["B"]}
    return out


def _list(params):
    with patch("shared.config_store.get_configured_resources", return_value=_configured(30, 2)):
        return json.loads(GetConfiguredResources.main(_req("GET", params=params, route="configured-resources")).get_body())


def test_type_counts_cover_everything_whatever_the_filter():
    answer = _list({"page": "1", "type": "MICROSOFT.KEYVAULT/VAULTS"})
    assert answer["type_counts"] == {"Microsoft.Web/sites": 30, "microsoft.keyvault/vaults": 2}
    assert answer["total"] == 2 and [r["name"] for r in answer["resources"]] == ["kv0", "kv1"]


def test_pages_are_bounded():
    answer = _list({"page": "9", "per_page": "25"})
    assert (answer["page"], answer["pages"], answer["total"]) == (2, 2, 32)
    assert len(answer["resources"]) == 7
    assert _list({"page": "1", "per_page": "100000"})["per_page"] == GetConfiguredResources.MAX_PER_PAGE


def test_without_a_page_everything_is_listed():
    answer = _list({})
    assert len(answer["resources"]) == 32 and "page" not in answer
    assert answer["resources"][0]["type"] == "Microsoft.Web/sites"


# ---------------------------------------------------------------- one spelling, checkpoints


@pytest.mark.parametrize("saved, asked", [
    (["kubeaudit"], "kube-audit"), (["kube-audit"], "kubeaudit"), (["Kube_Audit"], "KUBE AUDIT"),
])
def test_a_disabled_category_matches_in_any_spelling(saved, asked):
    with patch("shared.config_store.get_disabled_log_types", return_value=saved):
        assert config_store.is_log_type_disabled(asked) is True


def test_unreadable_checkpoints_skip_the_run():
    import BlobLogProcessor as blp
    service = MagicMock()
    service.get_container_client.return_value.download_blob.side_effect = RuntimeError("timeout")
    with patch.object(blp.BlobServiceClient, "from_connection_string", return_value=service):
        with pytest.raises(blp.CheckpointsUnavailable):
            blp._load_checkpoints("UseDevelopmentStorage=true")


def test_missing_checkpoints_are_empty():
    import BlobLogProcessor as blp
    from azure.core.exceptions import ResourceNotFoundError
    service = MagicMock()
    service.get_container_client.return_value.download_blob.side_effect = ResourceNotFoundError("missing")
    with patch.object(blp.BlobServiceClient, "from_connection_string", return_value=service):
        assert blp._load_checkpoints("UseDevelopmentStorage=true") == {}


@pytest.mark.parametrize("error, conflict", [
    (type("E", (Exception,), {"status_code": 412})(), True),
    (type("E", (Exception,), {"status_code": 500, "error_code": "BlobAlreadyExists"})(), True),
    (RuntimeError("request id 4120-409 failed"), False),
])
def test_conflicts_are_told_by_status_not_by_digits(error, conflict):
    assert config_store.is_conflict(error) is conflict


# ---------------------------------------------------------------- emptying the storage


def _account(name, purpose, owner="s247-diag-func-x"):
    acct = MagicMock()
    acct.name = name
    acct.primary_location = "eastus"
    acct.tags = {"managed-by": "s247-diag-logs", "purpose": purpose, "s247-collector": owner}
    return acct


def test_switching_off_empties_every_container_of_the_collectors_accounts_only(monkeypatch):
    monkeypatch.setenv("WEBSITE_SITE_NAME", "s247-diag-func-x")
    storage = MagicMock()
    storage.storage_accounts.list_by_resource_group.return_value = [
        _account("s247diageastusx", "diag-logs-regional"),
        _account("s247diagtenantx", "diag-logs-tenant"),
        _account("s247diagx", "function-app-runtime"),            # the app's own storage: untouched
        _account("s247diageastusy", "diag-logs-regional", owner="s247-diag-func-y"),  # another collector's
    ]
    service = MagicMock()
    service.list_containers.return_value = [{"name": "insights-logs"}, {"name": "insights-logs-auditevent"},
                                            {"name": "insights-activity-logs"}]
    region_mgr = MagicMock()
    region_mgr.set_retention.return_value = True
    with patch("azure.mgmt.storage.StorageManagementClient", return_value=storage), \
         patch("azure.storage.blob.BlobServiceClient", return_value=service), \
         patch("shared.collection._drop_checkpoints", return_value=True) as dropped:
        counts = collection._clear_storage(region_mgr, "rg")
    assert counts == {"accounts": 2, "containers_cleared": 6, "storage_errors": 0}
    dropped.assert_called_once_with(["s247diageastusx", "s247diagtenantx"])


def test_checkpoints_of_emptied_accounts_are_dropped(monkeypatch):
    import BlobLogProcessor as blp
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    checkpoints = {"s247diageastusx/insights-logs-a/b.json": {}, "other/insights-logs-a/c.json": {}}
    blob = MagicMock()
    service = MagicMock()
    service.get_container_client.return_value.get_blob_client.return_value = blob
    with patch.object(blp, "_load_checkpoints_with_etag", return_value=(checkpoints, "etag-1")), \
         patch("azure.storage.blob.BlobServiceClient.from_connection_string", return_value=service):
        assert collection._drop_checkpoints(["s247diageastusx"]) is True
    assert json.loads(blob.upload_blob.call_args.args[0]) == {"other/insights-logs-a/c.json": {}}


def _sweep(state, now):
    from datetime import datetime
    saved = []
    with patch("shared.config_store.get_collection_state", return_value=state), \
         patch("shared.config_store.update_collection_state", side_effect=lambda p: saved.append(p) or p), \
         patch("shared.collection._clear_all", return_value={"accounts": 1, "containers_cleared": 2, "storage_errors": 0}) as clear:
        result = collection.sweep_if_due(datetime.fromisoformat(now))
    return result, clear, saved


@pytest.mark.parametrize("state, now, due", [
    ({"enabled": False, "stop": {"state": "done", "finished_at": "2026-11-13T10:00:00+00:00"}}, "2026-11-13T10:40:00+00:00", True),
    ({"enabled": False, "stop": {"state": "done", "finished_at": "2026-11-13T10:00:00+00:00"}}, "2026-11-13T10:10:00+00:00", False),
    ({"enabled": False, "stop": {"state": "done", "finished_at": "2026-11-13T10:00:00+00:00",
                                 "swept_at": "2026-11-13T10:40:00+00:00"}}, "2026-11-13T10:50:00+00:00", False),
    ({"enabled": False, "stop": {"state": "done", "finished_at": "2026-11-12T08:00:00+00:00"}}, "2026-11-13T10:00:00+00:00", False),
    ({"enabled": False, "stop": {"state": "running", "started_at": "2026-11-13T10:00:00+00:00"}}, "2026-11-13T11:00:00+00:00", False),
    ({"enabled": True}, "2026-11-13T11:00:00+00:00", False),
])
def test_late_logs_are_swept_while_collection_is_off(state, now, due):
    result, clear, saved = _sweep(state, now)
    assert clear.called is due and (result is not None) is due
    if due:
        assert saved[0]["stop"]["swept_at"] == now  # claimed before sweeping


def test_a_storage_failure_makes_the_stop_partial():
    mgr = MagicMock()
    mgr.remove_all_diagnostic_settings.return_value = {"removed": 2, "skipped": 0, "errors": 0, "unresolved": 0,
                                                       "resolved_ids": ["a", "b"], "details": []}
    with patch("shared.config_store.update_collection_state", side_effect=lambda p: p), \
         patch("shared.subscriptions.monitored_subscriptions", return_value=([SUB], {}, None)), \
         patch("shared.azure_manager.AzureManager", return_value=mgr), \
         patch("shared.config_store.get_configured_resources", return_value={}), \
         patch("shared.config_store.unmark_resource_configured", return_value=True), \
         patch("shared.collection._clear_all", return_value={"accounts": 1, "containers_cleared": 0, "storage_errors": 1}), \
         patch("shared.debug_logger.log_event"), patch("shared.debug_logger.log_audit"):
        assert collection.stop_collection()["state"] == "partial"
