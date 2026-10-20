"""Per-collector diagnostic-setting names: no takeover or deletion across collectors."""

from unittest.mock import MagicMock

import pytest

from shared import azure_manager
from shared.azure_manager import AzureManager, LEGACY_SETTING_NAME, collector_setting_name

RES = "/subscriptions/s/resourceGroups/app-rg/providers/Microsoft.ApiManagement/service/apim"
OWN_SA = "s247diagwesteuropeoct14"
OTHER_SA = "s247diagwesteuropeqt4xn2"


def _sa_id(name):
    return f"/subscriptions/s/resourceGroups/s247-diag-logs-rg/providers/Microsoft.Storage/storageAccounts/{name}"


@pytest.fixture
def mgr(monkeypatch):
    monkeypatch.setenv("DIAG_STORAGE_SUFFIX", "oct14")
    m = AzureManager.__new__(AzureManager)
    m.settings = {}  # name -> storage account id on RES
    m.get_diagnostic_setting = lambda rid, name=None: (
        {"storage_account_id": m.settings[name or collector_setting_name()]}
        if (name or collector_setting_name()) in m.settings else None)
    def delete(rid, name=None):
        return m.settings.pop(name or collector_setting_name(), None) is not None
    m.delete_diagnostic_setting = MagicMock(side_effect=delete)
    return m


def test_setting_name_is_per_collector(monkeypatch):
    monkeypatch.setenv("DIAG_STORAGE_SUFFIX", "Oct14")
    assert collector_setting_name() == "s247-diag-logs-oct14"
    monkeypatch.delenv("DIAG_STORAGE_SUFFIX")
    assert collector_setting_name() == LEGACY_SETTING_NAME  # installs without a suffix keep the old name


def test_own_legacy_setting_is_removed_before_writing_ours(mgr):
    mgr.settings[LEGACY_SETTING_NAME] = _sa_id(OWN_SA)
    assert mgr.remove_legacy_setting_if_own(RES, OWN_SA) is True
    assert LEGACY_SETTING_NAME not in mgr.settings


def test_another_collectors_legacy_setting_is_left_alone(mgr):
    mgr.settings[LEGACY_SETTING_NAME] = _sa_id(OTHER_SA)
    assert mgr.remove_legacy_setting_if_own(RES, OWN_SA) is None
    assert mgr.settings[LEGACY_SETTING_NAME] == _sa_id(OTHER_SA)


def test_no_suffix_never_touches_the_legacy_name(mgr, monkeypatch):
    monkeypatch.delenv("DIAG_STORAGE_SUFFIX")
    mgr.settings[LEGACY_SETTING_NAME] = _sa_id(OWN_SA)
    assert mgr.remove_legacy_setting_if_own(RES, OWN_SA) is None
    assert LEGACY_SETTING_NAME in mgr.settings


def test_delete_own_settings_spares_other_collectors(mgr):
    mgr.settings["s247-diag-logs-oct14"] = _sa_id(OWN_SA)
    mgr.settings["s247-diag-logs-other"] = _sa_id(OTHER_SA)
    mgr.settings[LEGACY_SETTING_NAME] = _sa_id(OTHER_SA)  # another collector's old setting
    mgr.delete_own_settings(RES, {"storage_account": OWN_SA})
    assert set(mgr.settings) == {"s247-diag-logs-other", LEGACY_SETTING_NAME}


def test_delete_own_settings_removes_our_old_shared_name(mgr):
    mgr.settings[LEGACY_SETTING_NAME] = _sa_id(OWN_SA)
    mgr.delete_own_settings(RES, {"storage_account": OWN_SA})  # record from before the rename
    assert mgr.settings == {}


def test_bulk_removal_only_removes_this_collectors_settings(mgr, monkeypatch):
    monkeypatch.setenv("DIAG_SEED_REGION", "")
    monkeypatch.setattr(azure_manager.AzureManager, "get_all_resources",
                        lambda self, subs: [{"id": RES, "location": "westeurope"}], raising=False)
    mgr.get_all_resources = lambda subs: [{"id": RES, "location": "westeurope"}]
    mgr.settings["s247-diag-logs-oct14"] = _sa_id(OWN_SA)
    mgr.settings["s247-diag-logs-other"] = _sa_id(OTHER_SA)
    summary = mgr.remove_all_diagnostic_settings(["s"])
    assert summary["removed"] == 1
    assert set(mgr.settings) == {"s247-diag-logs-other"}


def test_failed_legacy_delete_is_reported_not_hidden(mgr):
    mgr.settings[LEGACY_SETTING_NAME] = _sa_id(OWN_SA)
    mgr.delete_diagnostic_setting.side_effect = lambda rid, name=None: False
    assert mgr.remove_legacy_setting_if_own(RES, OWN_SA) is False
    # A successful delete of the new name must not mask the failed legacy one.
    mgr.delete_diagnostic_setting.side_effect = lambda rid, name=None: name != LEGACY_SETTING_NAME
    assert mgr.delete_own_settings(RES, {"storage_account": OWN_SA}) is False


def test_bulk_removal_uses_the_recorded_storage_and_counts_failures(mgr):
    fallback = "s247diagwesteuropeleftover"  # the scan fell back to a tagged leftover account
    mgr.get_all_resources = lambda subs: [{"id": RES, "location": "westeurope"}]
    mgr.settings[LEGACY_SETTING_NAME] = _sa_id(fallback)
    summary = mgr.remove_all_diagnostic_settings(["s"], {RES: {"storage_account": fallback}})
    assert summary["removed"] == 1 and LEGACY_SETTING_NAME not in mgr.settings
    mgr.settings[LEGACY_SETTING_NAME] = _sa_id(fallback)
    mgr.delete_diagnostic_setting.side_effect = lambda rid, name=None: False
    summary = mgr.remove_all_diagnostic_settings(["s"], {RES: {"storage_account": fallback}})
    assert summary["errors"] == 1  # RemoveDiagSettings keeps tracking when errors > 0
