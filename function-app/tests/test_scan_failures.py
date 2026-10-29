"""Why a scan stopped, and why included resources got no diagnostic setting, reach Site24x7."""
from unittest.mock import patch

import pytest

import DiagSettingsManager as dsm
from shared import registration
from shared.ignore_list import IgnoreListUnavailable


def test_a_stopped_scan_records_its_step_and_reason():
    saved = {}
    with patch.object(dsm, "_run_scan", side_effect=IgnoreListUnavailable("x")), \
         patch("shared.config_store.get_scan_state", return_value={"in_progress": True, "current_phase": 2}), \
         patch("shared.config_store.update_scan_state", side_effect=lambda patch_: saved.update(patch_)):
        with pytest.raises(IgnoreListUnavailable):
            dsm.run_scan()
    assert saved["in_progress"] is False
    assert saved["last_scan_error"]["code"] == "filters_unavailable"
    assert saved["last_scan_error"]["phase"] == 2


def test_a_scan_without_subscriptions_records_not_configured():
    saved = {}
    with patch.object(dsm, "_run_scan", return_value={"error": "No SUBSCRIPTION_IDS configured"}), \
         patch("shared.config_store.get_scan_state", return_value={}), \
         patch("shared.config_store.update_scan_state", side_effect=lambda patch_: saved.update(patch_)):
        dsm.run_scan()
    assert saved["last_scan_error"]["code"] == "not_configured" and saved["last_scan_error"]["phase"] is None


@pytest.mark.parametrize("error, code", [
    (type("ClientAuthenticationError", (Exception,), {})(), "azure_access"),
    (type("ServiceRequestError", (Exception,), {})(), "network"),
    (ValueError("boom"), "unexpected"),
])
def test_failure_codes(error, code):
    assert dsm._scan_failure_code(error) == code


def test_registration_sends_only_safe_error_and_skip_reasons():
    assert registration._safe_scan_error({"code": "network", "phase": 1, "at": "2026-10-29T12:00:00Z"}) == \
        {"code": "network", "phase": 1, "at": "2026-10-29T12:00:00Z"}
    assert registration._safe_scan_error({"code": "rm -rf", "phase": 1}) is None
    assert registration._safe_scan_error({"code": "network", "phase": 9})["phase"] is None
    reasons = registration._safe_skip_reasons({
        "no_log_type": {"count": 5, "types": ["Microsoft.KeyVault/vaults", "<script>", "Microsoft.ServiceBus/namespaces"]},
        "bogus": {"count": 3, "types": []},
        "no_storage": {"count": 0, "types": []},
    })
    assert reasons == {"no_log_type": {"count": 5, "types": ["Microsoft.KeyVault/vaults", "Microsoft.ServiceBus/namespaces"]}}
