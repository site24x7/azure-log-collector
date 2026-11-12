"""A scan writes again a tracked setting that was deleted or changed in Azure, and a
resource that refuses its type's categories gets its own."""
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

import DiagSettingsManager

SUB = "00000000-0000-0000-0000-000000000001"
SITE = f"/subscriptions/{SUB}/resourceGroups/apps/providers/Microsoft.Web/sites/web1"
FUNC = f"/subscriptions/{SUB}/resourceGroups/apps/providers/Microsoft.Web/sites/func1"
SA = "s247diageastussfx"


def _resource(rid):
    return {"id": rid, "name": rid.rsplit("/", 1)[-1], "type": "Microsoft.Web/sites", "location": "eastus",
            "resource_group": "apps", "subscription_id": SUB, "tags": {}}


def _scan(resources, configured, azure_setting, categories_by_resource=None, scan_state=None, full=False,
          create_ok=None, lookup=None):
    """Run one scan against mocks; returns the AzureManager mock."""
    mgr = MagicMock()
    mgr.discovery_errors = []
    mgr.discovery_complete = True
    mgr.get_all_resources.return_value = resources
    mgr.get_diagnostic_setting.side_effect = lookup or (lambda rid, name=None, raise_errors=False: azure_setting.get(rid))
    cats = categories_by_resource or {}
    mgr.get_diagnostic_categories.side_effect = lambda rid: cats.get(rid, ["AppServiceHTTPLogs", "FunctionAppLogs"])
    mgr.create_diagnostic_setting.side_effect = create_ok or (lambda **kw: True)
    mgr.remove_legacy_setting_if_own.return_value = None
    regions = MagicMock()
    regions.get_active_regions.return_value = {"eastus"}
    regions.get_provisioned_regions.return_value = {"eastus": SA}
    regions.reconcile_regions.return_value = {"added": [], "idle": [], "errors": []}
    saved = {}
    with ExitStack() as stack:
        stack.enter_context(patch.dict("os.environ", {"DIAG_STORAGE_SUFFIX": "sfx", "RESOURCE_GROUP_NAME": "rg"}))
        for target, value in [
            ("shared.subscriptions.monitored_subscriptions", ([SUB], {}, None)),
            ("shared.azure_manager.AzureManager", mgr),
            ("shared.region_manager.RegionManager", regions),
            ("shared.ignore_list.load_ignore_list", {}),
            ("shared.config_store.get_configured_resources", configured),
            ("shared.config_store.get_resource_inventory", {"subscriptions": [SUB]}),
            ("shared.config_store.get_scan_state", scan_state or {}),
            ("shared.config_store.get_logtype_config", {"logType": "x"}),
            ("shared.config_store.is_log_type_disabled", False),
            ("shared.config_store.get_all_logtype_configs", {}),
            ("shared.config_store.get_entra_logtype_states", {}),
            ("DiagSettingsManager._refresh_supported_types", {}),
        ]:
            stack.enter_context(patch(target, return_value=value))
        for target in ("shared.site24x7_client.Site24x7Client", "shared.debug_logger.log_event",
                       "shared.config_store.clear_cache", "DiagSettingsManager._update_phase",
                       "DiagSettingsManager._save_early_scan_state", "shared.config_store.save_resource_inventory",
                       "shared.config_store.save_category_resource_types", "shared.config_store.save_category_names",
                       "shared.config_store.save_logtype_config"):
            stack.enter_context(patch(target))
        stack.enter_context(patch("shared.config_store.save_configured_resources",
                                  side_effect=lambda r: saved.update(r) or True))
        stack.enter_context(patch("shared.config_store.save_scan_state", side_effect=lambda s: saved.setdefault("_scan", s)))
        DiagSettingsManager.run_scan(full_reconcile=full)
    mgr.saved = saved
    return mgr


def _record(categories):
    return {"categories": categories, "storage_account": SA, "setting_name": "s247-diag-logs-sfx",
            "configured_at": "2026-11-01T00:00:00+00:00"}


def _in_azure(categories):
    return {"storage_account_id": f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/{SA}",
            "categories": categories}


CATS = ["AppServiceHTTPLogs", "FunctionAppLogs"]


def test_a_setting_deleted_in_azure_is_written_again():
    mgr = _scan([_resource(FUNC)], {FUNC: _record(CATS)}, azure_setting={}, full=True)
    assert mgr.create_diagnostic_setting.call_count == 1
    assert mgr.create_diagnostic_setting.call_args.kwargs["resource_id"] == FUNC
    assert mgr.saved["_scan"]["updated"] == 1


def test_a_setting_changed_in_azure_is_written_again():
    mgr = _scan([_resource(FUNC)], {FUNC: _record(CATS)}, azure_setting={FUNC: _in_azure(["FunctionAppLogs"])}, full=True)
    assert mgr.create_diagnostic_setting.call_count == 1


def test_a_matching_setting_is_left_alone():
    mgr = _scan([_resource(FUNC)], {FUNC: _record(CATS)}, azure_setting={FUNC: _in_azure(CATS)}, full=True)
    mgr.create_diagnostic_setting.assert_not_called()
    assert mgr.saved["_scan"]["already_configured"] == 1


def test_without_a_due_reconcile_the_record_is_trusted():
    state = {"last_full_reconcile": "2999-01-01T00:00:00+00:00"}
    mgr = _scan([_resource(FUNC)], {FUNC: _record(CATS)}, azure_setting={}, scan_state=state)
    mgr.get_diagnostic_setting.assert_not_called()
    mgr.create_diagnostic_setting.assert_not_called()


def test_an_unreadable_setting_is_trusted_this_time():
    def throttled(rid, name=None, raise_errors=False):
        raise RuntimeError("throttled")

    mgr = _scan([_resource(FUNC)], {FUNC: _record(CATS)}, azure_setting={}, full=True, lookup=throttled)
    mgr.create_diagnostic_setting.assert_not_called()
    # Not every setting was checked: the daily check is due again next scan.
    assert mgr.saved["_scan"]["last_full_reconcile"] == ""


def test_a_resource_refusing_its_types_categories_gets_its_own():
    # The type's sample is the function app; the web app has no FunctionAppLogs.
    own = {FUNC: CATS, SITE: ["AppServiceHTTPLogs"]}

    def create(**kw):
        return not (kw["resource_id"] == SITE and "FunctionAppLogs" in kw["categories"])

    mgr = _scan([_resource(FUNC), _resource(SITE)], {}, azure_setting={}, categories_by_resource=own,
                create_ok=create)
    written = {c.kwargs["resource_id"]: c.kwargs["categories"] for c in mgr.create_diagnostic_setting.call_args_list}
    assert written[SITE] == ["AppServiceHTTPLogs"]
    assert mgr.saved[SITE]["categories"] == ["AppServiceHTTPLogs"]
    assert mgr.saved["_scan"]["errors"] == 0


@pytest.mark.parametrize("own", [[], CATS])
def test_no_retry_when_the_own_categories_do_not_help(own):
    # Nothing different to try (none of its own, or the same ones): one attempt, one error.
    mgr = _scan([_resource(FUNC), _resource(SITE)], {}, azure_setting={},
                categories_by_resource={FUNC: CATS, SITE: own},
                create_ok=lambda **kw: kw["resource_id"] != SITE)
    site_calls = [c for c in mgr.create_diagnostic_setting.call_args_list if c.kwargs["resource_id"] == SITE]
    assert len(site_calls) == 1
    assert mgr.saved["_scan"]["errors"] == 1
