"""Filter choices come from the last scan, never from a live listing per request."""

import json
from unittest.mock import patch

import azure.functions as func

import GetIgnoreList
from shared import config_store

RESOURCES = [
    {"id": "/subscriptions/s1/resourceGroups/rg-a/providers/Microsoft.Web/sites/a", "type": "Microsoft.Web/sites",
     "location": "westeurope", "resource_group": "rg-a", "tags": {"env": "prod", "team": "core"}},
    {"id": "/subscriptions/s1/resourceGroups/rg-b/providers/Microsoft.KeyVault/vaults/b", "type": "Microsoft.KeyVault/vaults",
     "location": "eastus", "resource_group": "rg-b", "tags": {"env": "prod"}},
    {"id": "/subscriptions/s1/resourceGroups/rg-b/providers/Microsoft.KeyVault/vaults/c", "type": "Microsoft.KeyVault/vaults",
     "location": "", "resource_group": "rg-b", "tags": None},
]


def test_inventory_keeps_distinct_sorted_values_only():
    written = {}
    with patch.object(config_store, "_write_blob", side_effect=lambda p, d: written.update({p: json.loads(d)}) or True):
        assert config_store.save_resource_inventory(RESOURCES, ["s1"]) is True
    inv = written[config_store.RESOURCE_INVENTORY_BLOB]
    assert inv["resource_groups"] == ["rg-a", "rg-b"]
    assert inv["locations"] == ["eastus", "westeurope"]
    assert inv["resource_types"] == ["Microsoft.KeyVault/vaults", "Microsoft.Web/sites"]
    assert inv["tags"] == ["env=prod", "team=core"]
    assert inv["subscriptions"] == ["s1"] and inv["resource_count"] == 3
    assert "id" not in json.dumps(inv)          # no per-resource list


def test_inventory_values_are_capped(monkeypatch):
    monkeypatch.setattr(config_store, "INVENTORY_MAX_VALUES", 2)
    many = [{"resource_group": f"rg-{i}"} for i in range(5)]
    written = {}
    with patch.object(config_store, "_write_blob", side_effect=lambda p, d: written.update({p: json.loads(d)}) or True):
        config_store.save_resource_inventory(many, [])
    assert written[config_store.RESOURCE_INVENTORY_BLOB]["resource_groups"] == ["rg-0", "rg-1"]


def _ignore_list(inventory):
    req = func.HttpRequest(method="GET", url="/api/ignore-list", body=b"", headers={})
    with patch("shared.ignore_list.load_ignore_list", return_value={"tags": {"include": [], "exclude": []}}), \
         patch("shared.config_store.get_resource_inventory", return_value=inventory), \
         patch("shared.azure_manager.AzureManager") as live:
        resp = GetIgnoreList.main(req)
    live.assert_not_called()                     # never lists the subscription
    return resp.status_code, json.loads(resp.get_body())


def test_ignore_list_answers_from_the_inventory(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_IDS", "s1")
    status, body = _ignore_list({"updated_at": "2026-10-22T07:29:06Z", "resource_groups": ["rg-a"], "locations": ["eastus"],
                                 "resource_types": ["Microsoft.Web/sites"], "tags": ["env=prod"], "subscriptions": ["s1"]})
    assert status == 200
    assert body["available"]["resource_groups"] == ["rg-a"] and body["available"]["tags"] == ["env=prod"]
    assert body["available"]["resource_ids"] == [] and body["available_updated_at"] == "2026-10-22T07:29:06Z"


def test_ignore_list_before_the_first_scan_has_empty_choices(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_IDS", "s1,s2")
    status, body = _ignore_list(None)
    assert status == 200 and body["available"]["resource_groups"] == []
    assert body["available"]["subscriptions"] == ["s1", "s2"] and body["available_updated_at"] is None


def test_sdk_request_logging_is_kept_to_warnings():
    import logging
    import shared  # noqa: F401  (sets the levels on import)
    assert logging.getLogger("azure").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("azure.core.pipeline.policies.http_logging_policy").getEffectiveLevel() >= logging.WARNING
