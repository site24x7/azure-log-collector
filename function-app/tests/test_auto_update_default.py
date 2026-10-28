"""Automatic installs are off by default; the timer only checks, admins install."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import AutoUpdater
import CheckUpdate
from shared.updater import automatic_updates_enabled


def _run_timer():
    with patch("shared.updater.check_and_apply_update",
               return_value={"action": "update_available", "remote_version": "9.9.9"}) as check, \
         patch("shared.debug_logger.log_event") as event:
        AutoUpdater.main(MagicMock(past_due=False))
    return check, event


def test_timer_only_checks_when_the_setting_is_absent(monkeypatch):
    monkeypatch.delenv("AUTO_UPDATE_ENABLED", raising=False)
    check, event = _run_timer()
    check.assert_called_once_with(auto_apply=False)
    assert event.call_args.args[3]["automatic_install"] is False


@pytest.mark.parametrize("value", ["false", "0", "", "no", "off"])
def test_anything_but_true_keeps_installs_manual(monkeypatch, value):
    monkeypatch.setenv("AUTO_UPDATE_ENABLED", value)
    assert automatic_updates_enabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
def test_admins_can_turn_automatic_installs_on(monkeypatch, value):
    monkeypatch.setenv("AUTO_UPDATE_ENABLED", value)
    check, _ = _run_timer()
    check.assert_called_once_with(auto_apply=True)


def test_manual_update_still_installs_with_automatic_off(monkeypatch):
    monkeypatch.delenv("AUTO_UPDATE_ENABLED", raising=False)
    request = MagicMock(method="POST", params={"apply": "1"})
    with patch("shared.updater.check_and_apply_update", return_value={"action": "deployed"}) as check:
        CheckUpdate.main(request)
    check.assert_called_once_with(auto_apply=True)


def test_template_deploys_with_automatic_installs_off():
    template = json.loads((Path(__file__).resolve().parents[2] / "setup/azuredeploy.json").read_text())
    settings = {}

    def walk(node):
        if isinstance(node, dict):
            if node.get("name") == "AUTO_UPDATE_ENABLED" and "value" in node:
                settings["AUTO_UPDATE_ENABLED"] = node["value"]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(template)
    assert settings == {"AUTO_UPDATE_ENABLED": "false"}
