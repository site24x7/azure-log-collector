"""A collector never collects from another Site24x7 collector's resources.

A deployment once set diagnostic settings on a second collector's Function App
and Application Insights; deleting it then failed, because the second
collector's resource-group lock refused to remove those settings.
"""

from unittest.mock import patch

import DiagSettingsManager as dsm

OWN_RG = "s247-diag-logs-deltest-rg"
TAG = {"managed-by": "s247-diag-logs"}


def _res(name, rg, tags=None):
    return {"id": f"/subscriptions/s/resourceGroups/{rg}/providers/Microsoft.Web/sites/{name}",
            "name": name, "resource_group": rg, "tags": tags}


def test_other_collectors_resources_are_never_collected():
    resources = [_res("app", "app-rg"), _res("s247-diag-func-qt4xn2", "s247-diag-logs-rg", TAG)]
    for include_own in (False, True):
        kept, own, others = dsm.exclude_collector_resources(resources, OWN_RG, include_own)
        assert [r["name"] for r in kept] == ["app"]
        assert (own, others) == (0, 1)


def test_own_resources_follow_the_switch():
    resources = [_res("s247-diag-func-deltest", OWN_RG.upper(), TAG), _res("app", "app-rg")]
    kept, own, others = dsm.exclude_collector_resources(resources, OWN_RG, include_own=False)
    assert [r["name"] for r in kept] == ["app"] and (own, others) == (1, 0)
    kept, own, others = dsm.exclude_collector_resources(resources, OWN_RG, include_own=True)
    assert [r["name"] for r in kept] == ["s247-diag-func-deltest", "app"] and (own, others) == (0, 0)


def test_only_the_collector_tag_marks_a_collector_resource():
    resources = [_res("a", "app-rg", {"managed-by": "someone-else"}), _res("b", "app-rg", None),
                 _res("c", "app-rg", {"purpose": "diag-logs-regional"})]
    kept, own, others = dsm.exclude_collector_resources(resources, OWN_RG, include_own=False)
    assert [r["name"] for r in kept] == ["a", "b", "c"] and others == 0


def test_status_confirms_a_running_update():
    import GetStatus
    with patch("shared.updater.confirm_running_version") as confirm, \
            patch("shared.updater.get_update_state", return_value={"pending_version": "1.1.0-beta13"}):
        state = GetStatus._update_state()
    confirm.assert_called_once()
    assert state["pending_version"] == "1.1.0-beta13"


def test_status_still_reports_when_confirmation_fails():
    import GetStatus
    with patch("shared.updater.confirm_running_version", side_effect=RuntimeError("storage")), \
            patch("shared.updater.get_update_state", return_value={"pending_version": "1.1.0-beta13"}):
        state = GetStatus._update_state()
    assert state["pending_version"] == "1.1.0-beta13"
