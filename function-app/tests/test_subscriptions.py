"""Subscription discovery keeps deployment order and reports incomplete discovery."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared import subscriptions

HOST = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONFIGURED = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ALPHA = "cccccccc-cccc-cccc-cccc-cccccccccccc"
ZULU = "dddddddd-dddd-dddd-dddd-dddddddddddd"
DISABLED = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def test_configured_ids_are_normalized_deduplicated_and_keep_order(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_IDS", f" {HOST.upper()},bad,{CONFIGURED},{HOST},,")
    assert subscriptions.configured_subscription_ids() == [HOST, CONFIGURED]


@pytest.mark.parametrize("endpoint", ["IDENTITY_ENDPOINT", "MSI_ENDPOINT"])
def test_enabled_subscriptions_are_added_in_name_order(endpoint, monkeypatch):
    monkeypatch.setenv(endpoint, "http://identity")
    monkeypatch.setenv("SUBSCRIPTION_IDS", f"{HOST},{CONFIGURED}")
    listed = [SimpleNamespace(subscription_id=s, display_name=n, state=state) for s, n, state in [
        (ZULU, "Zulu", "Enabled"), (DISABLED, "Disabled", "Disabled"),
        (ALPHA.upper(), "alpha", "Enabled"), (HOST, "Hosting", "Enabled"),
        (CONFIGURED, "Configured", "Enabled"), (ALPHA, "alpha", "Enabled"),
        ("invalid", "Invalid", "Enabled"),
    ]]
    with patch("azure.mgmt.resource.SubscriptionClient") as client, \
         patch("azure.identity.DefaultAzureCredential") as credential:
        client.return_value.subscriptions.list.return_value = listed
        ids, names, error = subscriptions.monitored_subscriptions()
    client.assert_called_once_with(credential.return_value)
    assert ids == [HOST, CONFIGURED, ALPHA, ZULU]
    assert names == {HOST: "Hosting", CONFIGURED: "Configured", ALPHA: "alpha", ZULU: "Zulu"}
    assert error is None


def test_without_managed_identity_only_configured_ids_are_used(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_IDS", HOST)
    with patch("azure.mgmt.resource.SubscriptionClient") as client:
        assert subscriptions.monitored_subscriptions() == ([HOST], {}, None)
    client.assert_not_called()


def test_listing_failure_discards_partial_results_and_reports_error(monkeypatch):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://identity")
    monkeypatch.setenv("SUBSCRIPTION_IDS", HOST)

    def partial_list():
        yield SimpleNamespace(subscription_id=ALPHA, display_name="Alpha", state="Enabled")
        raise RuntimeError("listing failed")

    with patch("azure.mgmt.resource.SubscriptionClient") as client, \
         patch("azure.identity.DefaultAzureCredential"):
        client.return_value.subscriptions.list.return_value = partial_list()
        assert subscriptions.monitored_subscriptions() == ([HOST], {}, "subscription list: RuntimeError")
