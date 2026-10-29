"""PUT /api/ignore-list: validation, failed saves, and which settings it removes at once."""

import json
from unittest.mock import MagicMock, patch

import azure.functions as func
import pytest

import UpdateIgnoreList
from shared.ignore_list import IgnoreListUnavailable, load_ignore_list

RES_A = "/subscriptions/s1/resourceGroups/rg-a/providers/Microsoft.Web/sites/a"
RES_B = "/subscriptions/s2/resourceGroups/rg-b/providers/Microsoft.Web/sites/b"
EMPTY = {"include_locations": [], "resource_ids": [], "include_subscriptions": [],
         "tags": {"include": [], "exclude": []}, "include_resource_types": []}


def _req(body):
    return func.HttpRequest(method="PUT", url="/api/ignore-list",
                            body=json.dumps(body).encode(), headers={})


def _run(body, previous=EMPTY, saved=True, deleted=True, unmarked=True, scan_running=False):
    queue = MagicMock()
    mgr = MagicMock()
    mgr.return_value.delete_own_settings.return_value = deleted
    load = MagicMock(side_effect=previous) if isinstance(previous, Exception) else MagicMock(return_value=previous)
    with patch("shared.ignore_list.load_ignore_list", load), \
         patch("shared.ignore_list.save_ignore_list", return_value=saved), \
         patch("shared.config_store.get_configured_resources", return_value={RES_A: {}, RES_B: {}}), \
         patch("shared.config_store.unmark_resource_configured", return_value=unmarked) as unmark, \
         patch("shared.azure_manager.AzureManager", mgr), \
         patch("shared.config_store.get_scan_state", return_value={"in_progress": scan_running}), \
         patch("shared.debug_logger.log_audit"):
        resp = UpdateIgnoreList.main(_req(body), queue)
    return resp, mgr.return_value.delete_own_settings, unmark, queue


def test_include_tag_rule_removes_nothing_now_and_queues_a_scan():
    # Before: a resource ID alone never matched the include tag, so every setting was removed.
    resp, delete, _, queue = _run({"tags": {"include": ["env=prod"], "exclude": []}})
    assert resp.status_code == 200
    delete.assert_not_called()
    queue.set.assert_called_once()
    assert json.loads(resp.get_body())["scan_queued"] is True


def test_resource_id_rule_removes_only_that_resource_at_once():
    resp, delete, unmark, queue = _run({"resource_ids": [RES_A.upper()]})
    assert [c.args[0] for c in delete.call_args_list] == [RES_A]
    unmark.assert_called_once_with(RES_A)
    queue.set.assert_not_called()
    assert json.loads(resp.get_body())["diag_settings_removed"] == 1


def test_setting_that_could_not_be_removed_keeps_its_record():
    resp, _, unmark, _ = _run({"resource_ids": [RES_B]}, deleted=False)
    unmark.assert_not_called()
    body = json.loads(resp.get_body())
    assert (body["diag_settings_removed"], body["diag_settings_not_removed"]) == (0, 1)


def test_failed_save_is_reported():
    resp, delete, _, _ = _run({"resource_ids": [RES_A]}, saved=False)
    assert resp.status_code == 500
    delete.assert_not_called()


@pytest.mark.parametrize("body", [
    {"include_locations": None},
    {"resource_ids": None},
    {"tags": {"exclude": None}},
    {"tags": {"include": [], "other": []}},
    {"tags": "env=dev"},
])
def test_null_or_malformed_lists_are_rejected(body):
    resp = UpdateIgnoreList.main(_req(body), MagicMock())
    assert resp.status_code == 400


def test_scan_stops_when_the_ignore_list_cannot_be_read():
    blob = MagicMock()
    blob.download_blob.side_effect = RuntimeError("storage unavailable")
    with patch("shared.ignore_list._get_blob_client", return_value=blob):
        assert load_ignore_list() == EMPTY            # dashboard reads still degrade gracefully
        with pytest.raises(IgnoreListUnavailable):
            load_ignore_list(strict=True)             # scans do not configure excluded resources


def test_changing_a_tag_key_case_queues_no_scan():
    # Tag names match case-insensitively (as in Azure), so Env=prod -> env=prod excludes the same resources.
    previous = dict(EMPTY, tags={"include": [], "exclude": ["Env=prod"]})
    _, _, _, queue = _run({"tags": {"include": [], "exclude": ["env=prod"]}}, previous=previous)
    queue.set.assert_not_called()


def test_a_new_tag_rule_queues_a_scan():
    previous = dict(EMPTY, tags={"include": [], "exclude": ["env=prod"]})
    _, _, _, queue = _run({"tags": {"include": [], "exclude": ["env=prod", "owner"]}}, previous=previous)
    queue.set.assert_called_once()


def test_reordering_rules_queues_no_scan():
    previous = dict(EMPTY, include_locations=["westeurope", "eastus"])
    _, _, _, queue = _run({"include_locations": ["EastUS", "westeurope"]}, previous=previous)
    queue.set.assert_not_called()


def test_unreadable_previous_list_queues_a_scan():
    _, _, _, queue = _run({"resource_ids": [RES_A]}, previous=IgnoreListUnavailable("x"))
    queue.set.assert_called_once()


def test_busy_scan_request_is_retried_not_dropped():
    import ScanWorker
    msg = MagicMock(dequeue_count=1)
    msg.get_body.return_value = b"{}"
    with patch("shared.config_store.try_acquire_scan_lock", return_value=False), \
         patch("shared.debug_logger.log_event"), \
         patch("DiagSettingsManager.run_scan") as scan:
        with pytest.raises(RuntimeError):
            ScanWorker.main(msg)
    scan.assert_not_called()


def test_narrowing_subscriptions_removes_only_unselected_subscription(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_IDS", "hosting,s1,s2")
    resp, delete, unmark, queue = _run({"include_subscriptions": ["s2"]},
                                     previous=dict(EMPTY, include_subscriptions=["s1", "s2"]))
    assert resp.status_code == 200
    assert [call.args[0] for call in delete.call_args_list] == [RES_A]
    assert [call.args[0] for call in unmark.call_args_list] == [RES_A]
    queue.set.assert_called_once()
    assert json.loads(resp.get_body())["diag_settings_removed"] == 1


@pytest.mark.parametrize("selected", [["s2"], []])
def test_subscription_change_queues_scan_without_removing_hosting_resources(selected, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_IDS", "s1,s2")
    resp, delete, _, queue = _run({"include_subscriptions": selected},
                                 previous=dict(EMPTY, include_subscriptions=["s1", "s2"]))
    assert resp.status_code == 200
    delete.assert_not_called()
    queue.set.assert_called_once()


@pytest.mark.parametrize("rule", [
    {"include_locations": ["eastus"]},
    {"include_resource_types": ["Microsoft.Web/sites"]},
])
def test_detail_include_changes_queue_scan_without_id_only_removal(rule):
    resp, delete, _, queue = _run(rule)
    assert resp.status_code == 200
    delete.assert_not_called()
    queue.set.assert_called_once()


def test_unchanged_filters_queue_another_scan_when_one_is_running():
    resp, delete, _, queue = _run({}, scan_running=True)
    assert resp.status_code == 200
    delete.assert_not_called()
    queue.set.assert_called_once()
