"""Delete Collector: only after every setting is gone, only an all-own resource group, own locks only."""
import json
from unittest.mock import MagicMock, patch

import azure.functions as func
import pytest

import DeleteCollector
import ScanWorker
from shared import teardown

SUB = "00000000-0000-0000-0000-000000000001"
APP = "s247-diag-func-qt4xn2"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("WEBSITE_SITE_NAME", APP)
    monkeypatch.setenv("DIAG_STORAGE_SUFFIX", "qt4xn2")
    monkeypatch.setenv("SUBSCRIPTION_IDS", SUB)
    monkeypatch.setenv("RESOURCE_GROUP_NAME", "s247-diag-logs-rg")


def _res(name, rtype, tags=None, location="eastus"):
    r = MagicMock()
    r.name, r.type, r.tags, r.location = name, rtype, tags, location
    return r


MANAGED = {"managed-by": "s247-diag-logs"}
OWN = [
    _res(APP, "Microsoft.Web/sites", MANAGED),
    _res("s247-diag-func-qt4xn2-plan", "Microsoft.Web/serverFarms", MANAGED),
    _res("s247diagqt4xn2", "Microsoft.Storage/storageAccounts", dict(MANAGED, purpose="function-app-runtime")),
    _res("s247diageastusqt4xn2", "Microsoft.Storage/storageAccounts",
         dict(MANAGED, purpose="diag-logs-regional", region="eastus", **{"s247-collector": APP})),
    _res("s247-diag-func-qt4xn2-ai", "microsoft.insights/components", MANAGED),
    _res("s247-diag-func-qt4xn2-logs", "Microsoft.OperationalInsights/workspaces", MANAGED),
    _res("Failure Anomalies - s247-diag-func-qt4xn2-ai", "microsoft.alertsmanagement/smartDetectorAlertRules"),
    _res("Application Insights Smart Detection", "microsoft.insights/actiongroups"),
]


def test_a_group_of_only_the_collectors_resources_passes():
    assert teardown.foreign_resources(OWN) == []


@pytest.mark.parametrize("extra", [
    _res("customer-vm", "Microsoft.Compute/virtualMachines"),
    _res("s247-diag-func-other1", "Microsoft.Web/sites", MANAGED),  # another collector in the same group
    _res("s247diageastusother1", "Microsoft.Storage/storageAccounts",
         dict(MANAGED, purpose="diag-logs-regional", **{"s247-collector": "s247-diag-func-other1"})),
])
def test_anything_else_in_the_group_stops_the_delete(extra):
    assert teardown.foreign_resources(OWN + [extra]) == [f"{extra.name} ({extra.type})"]


def _req(body):
    return func.HttpRequest(method="POST", url="/api/delete-collector", params={},
                            body=json.dumps(body).encode())


@pytest.mark.parametrize("body", [{}, {"confirm": "other-app"}, {"confirm": 1}, ["x"]])
def test_the_endpoint_needs_this_apps_name(body):
    out = MagicMock()
    assert DeleteCollector.main(_req(body), out).status_code == 400
    out.set.assert_not_called()


def test_the_endpoint_queues_the_job():
    out = MagicMock()
    with patch("shared.config_store.update_collection_state", side_effect=lambda p: p), \
         patch("shared.debug_logger.log_audit"):
        assert DeleteCollector.main(_req({"confirm": APP.upper()}), out).status_code == 202
    assert json.loads(out.set.call_args.args[0])["action"] == teardown.DELETE_ACTION


def _run(stop_state="done", resources=OWN, locks=None, lock_error=None):
    saved = []
    rm = MagicMock()
    rm.resources.list_by_resource_group.return_value = resources
    lock_client = MagicMock()
    lock = MagicMock()
    lock.name, lock.id = "s247-lock-rg", f"/subscriptions/{SUB}/resourceGroups/s247-diag-logs-rg/providers/Microsoft.Authorization/locks/s247-lock-rg"
    lock_client.management_locks.list_at_resource_group_level.return_value = locks if locks is not None else [lock]
    if lock_error:
        lock_client.management_locks.delete_by_scope.side_effect = lock_error
    with patch("shared.config_store.update_collection_state", side_effect=lambda p: saved.append(p) or p), \
         patch("shared.collection.request_change", return_value={}), \
         patch("shared.collection.stop_collection", return_value={"state": stop_state, "errors": 1}), \
         patch("azure.identity.DefaultAzureCredential"), \
         patch("azure.mgmt.resource.ResourceManagementClient", return_value=rm), \
         patch("azure.mgmt.resource.locks.ManagementLockClient", return_value=lock_client), \
         patch("shared.registration.register_with_site24x7") as report, \
         patch("shared.debug_logger.log_event"), patch("shared.debug_logger.log_audit"):
        outcome = teardown.delete_collector()
    return outcome, rm, lock_client, report, saved


def test_the_group_is_deleted_after_settings_and_locks_go():
    outcome, rm, locks, report, saved = _run()
    assert outcome["state"] == "deleting"
    locks.management_locks.delete_by_scope.assert_called_once()
    rm.resource_groups.begin_delete.assert_called_once_with("s247-diag-logs-rg")
    # Site24x7 is told before the delete starts (the app may stop at any moment after).
    assert any(p.get("delete", {}).get("state") == "deleting" for p in saved)
    assert report.called


def test_nothing_is_deleted_while_a_setting_remains():
    outcome, rm, locks, _, _ = _run(stop_state="partial")
    assert (outcome["state"], outcome["reason"]) == ("failed", "settings_remain")
    rm.resources.list_by_resource_group.assert_not_called()
    rm.resource_groups.begin_delete.assert_not_called()


def test_a_group_with_other_resources_is_kept():
    outcome, rm, locks, _, _ = _run(resources=OWN + [_res("customer-db", "Microsoft.Sql/servers")])
    assert outcome["reason"] == "foreign_resources" and outcome["foreign"] == ["customer-db (Microsoft.Sql/servers)"]
    locks.management_locks.delete_by_scope.assert_not_called()
    rm.resource_groups.begin_delete.assert_not_called()


def test_a_lock_that_cannot_be_removed_stops_the_delete():
    error = type("HttpResponseError", (Exception,), {"status_code": 403})()
    outcome, rm, _, _, _ = _run(lock_error=error)
    assert outcome["reason"] == "locks" and outcome["locks_failed"] == ["s247-lock-rg: 403"]
    rm.resource_groups.begin_delete.assert_not_called()


def test_a_lock_someone_else_made_is_left_and_stops_the_delete():
    other = MagicMock()
    other.name, other.id = "customer-lock", "/x/providers/Microsoft.Authorization/locks/customer-lock"
    outcome, rm, locks, _, _ = _run(locks=[other])
    assert outcome["reason"] == "locks" and outcome["other_locks"] == ["customer-lock"]
    locks.management_locks.delete_by_scope.assert_not_called()
    rm.resource_groups.begin_delete.assert_not_called()


def test_the_worker_runs_the_delete_under_the_scan_lock():
    msg = MagicMock()
    msg.get_body.return_value = json.dumps({"action": teardown.DELETE_ACTION}).encode()
    msg.dequeue_count = 1
    with patch("shared.config_store.try_acquire_scan_lock", return_value=True), \
         patch("shared.teardown.delete_collector", return_value={"state": "deleting"}) as delete, \
         patch("ScanWorker._clear_in_progress"), \
         patch("shared.registration.register_with_site24x7") as report, \
         patch("shared.debug_logger.log_event"):
        ScanWorker.main(msg)
    delete.assert_called_once()
    report.assert_not_called()  # already told Site24x7 before the group delete started
