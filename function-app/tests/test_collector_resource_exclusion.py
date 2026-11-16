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


LOCKED = ("(ScopeLocked) The scope '/subscriptions/s/resourceGroups/s247-diag-logs-rg/providers/Microsoft.Web/sites/"
          "s247-diag-func-qt4xn2/providers/Microsoft.Insights/diagnosticSettings/x' cannot perform delete operation "
          "because following scope(s) are locked: '/subscriptions/s/resourcegroups/s247-diag-logs-rg'. Please remove")


def test_blocking_details_name_the_resources_and_the_locked_groups():
    from shared.collection import blocking_details
    details = [
        {"id": "/subscriptions/s/resourceGroups/s247-diag-logs-rg/providers/Microsoft.Web/sites/s247-diag-func-qt4xn2",
         "status": "error", "message": LOCKED},
        {"id": "/subscriptions/s/resourceGroups/s247-diag-logs-rg/providers/Microsoft.Insights/components/qt4xn2-ai",
         "status": "error", "message": LOCKED},
        {"id": "/subscriptions/s/resourceGroups/app/providers/Microsoft.Web/sites/app", "status": "error"},
        {"id": "/subscriptions/s/resourceGroups/app/providers/Microsoft.Web/sites/done", "status": "removed"},
    ]
    blocked, groups = blocking_details(details)
    assert blocked == ["s247-diag-func-qt4xn2", "qt4xn2-ai", "app"]
    assert groups == ["s247-diag-logs-rg"]
    assert blocking_details([]) == ([], [])


def test_a_delete_stopped_by_settings_reports_what_blocked_it(monkeypatch):
    from shared import teardown
    saved = {}
    monkeypatch.setattr(teardown, "_save", lambda patch: saved.update(patch))
    monkeypatch.setattr(teardown, "_hosting_subscription", lambda: "s")
    monkeypatch.setattr("shared.collection.request_change", lambda enabled: {"enabled": enabled})
    monkeypatch.setattr("shared.collection.stop_collection", lambda: {
        "state": "partial", "errors": 2, "unresolved": 0,
        "blocked": ["s247-diag-func-qt4xn2", "qt4xn2-ai"], "locked_groups": ["s247-diag-logs-rg"]})
    monkeypatch.setattr("shared.registration.register_with_site24x7", lambda timeout=30: None)
    outcome = teardown.delete_collector()
    assert outcome["state"] == "failed" and outcome["reason"] == "settings_remain"
    assert outcome["blocked"] == ["s247-diag-func-qt4xn2", "qt4xn2-ai"]
    assert outcome["locked_groups"] == ["s247-diag-logs-rg"]
