"""PUT /api/ignore-list: validation, failed saves, and which settings it removes at once."""

import json
from unittest.mock import MagicMock, patch

import azure.functions as func
import pytest

import UpdateIgnoreList
from shared.ignore_list import IgnoreListUnavailable, load_ignore_list

RES_A = "/subscriptions/s1/resourceGroups/rg-a/providers/Microsoft.Web/sites/a"
RES_B = "/subscriptions/s1/resourceGroups/rg-b/providers/Microsoft.Web/sites/b"
EMPTY = {"resource_groups": [], "locations": [], "resource_ids": [], "subscriptions": [],
         "tags": {"include": [], "exclude": []}, "resource_types": []}


def _req(body):
    return func.HttpRequest(method="PUT", url="/api/ignore-list",
                            body=json.dumps(body).encode(), headers={})


def _run(body, previous=EMPTY, saved=True, deleted=True, unmarked=True):
    stored = dict(EMPTY, **body)
    if isinstance(stored.get("tags"), list):
        stored["tags"] = {"include": [], "exclude": stored["tags"]}
    queue = MagicMock()
    mgr = MagicMock()
    mgr.return_value.delete_own_settings.return_value = deleted
    with patch("shared.ignore_list.load_ignore_list", side_effect=[previous, stored]), \
         patch("shared.ignore_list.save_ignore_list", return_value=saved), \
         patch("shared.config_store.get_configured_resources", return_value={RES_A: {}, RES_B: {}}), \
         patch("shared.config_store.unmark_resource_configured", return_value=unmarked) as unmark, \
         patch("shared.azure_manager.AzureManager", mgr), \
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


def test_resource_group_rule_removes_only_that_group_at_once():
    resp, delete, unmark, queue = _run({"resource_groups": ["RG-A"]})
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
    resp, delete, _, _ = _run({"resource_groups": ["rg-a"]}, saved=False)
    assert resp.status_code == 500
    delete.assert_not_called()


@pytest.mark.parametrize("body", [
    {"locations": None},
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
