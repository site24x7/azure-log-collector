"""Collector self-registration with Site24x7 (shared/registration.py)."""

import json
import logging
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from shared import registration
from shared.site24x7_client import Site24x7Client

SECRET = "fn-key-should-never-be-logged"
SITE_ID = ("/subscriptions/sub-1/resourceGroups/s247-diag-logs-rg"
           "/providers/Microsoft.Web/sites/s247-diag-func-oct14")


@pytest.fixture
def azure_env(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_IDS", "sub-1")
    monkeypatch.setenv("RESOURCE_GROUP_NAME", "s247-diag-logs-rg")
    monkeypatch.setenv("WEBSITE_SITE_NAME", "s247-diag-func-oct14")
    monkeypatch.setenv("WEBSITE_HOSTNAME", "s247-diag-func-oct14.azurewebsites.net")
    monkeypatch.setenv("REGION_NAME", "West Europe")
    monkeypatch.setenv("SITE24X7_API_KEY", "device-key-secret")


@pytest.fixture
def no_state():
    with patch("shared.config_store.get_scan_state", return_value={
            "last_scan_time": "2026-10-14T11:32:59+00:00", "total_resources": 155,
            "active_resources": 152, "ignored_resources": 3, "errors": []}), \
         patch("shared.debug_logger.get_processing_stats", return_value=[
            {"timestamp": "2026-10-14T11:40:00+00:00", "uploaded": 7, "dropped": 1, "retrying": 4},
            {"timestamp": "2026-10-14T11:30:00+00:00", "uploaded": 3, "dropped": 0, "retrying": 900}]):
        yield


def test_registration_fields_come_from_the_app_environment(azure_env, no_state):
    with patch.object(registration, "get_function_key", return_value=SECRET):
        fields = registration.build_registration()
    assert fields["functionKey"] == SECRET
    assert fields["resourceUrl"] == "https://s247-diag-func-oct14.azurewebsites.net"
    assert fields["resourceId"] == SITE_ID
    assert (fields["functionAppName"], fields["subscriptionId"], fields["resourceGroup"]) == \
        ("s247-diag-func-oct14", "sub-1", "s247-diag-logs-rg")
    assert fields["region"] == "West Europe"
    status = json.loads(fields["status"])
    assert status["total_resources"] == 155
    assert (status["recent_uploaded"], status["recent_dropped"]) == (10, 1)
    # Only the latest run's failed uploads are pending; older ones were re-sent since.
    assert status["pending_records"] == 4
    assert status["last_processing_time"] == "2026-10-14T11:40:00+00:00"
    assert SECRET not in fields["status"]


def test_outside_azure_registration_is_skipped(monkeypatch):
    monkeypatch.delenv("WEBSITE_HOSTNAME", raising=False)
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    assert registration.build_registration() is None


class _FakeArm:
    """Function keys per function, as ARM serves them."""

    def __init__(self, keys):
        self.keys = keys            # function -> {key name: value}
        self.calls = []

    def request(self, method, url, headers=None, json=None, timeout=None, allow_redirects=True):
        self.calls.append((method, url))
        function = url.split("/functions/")[1].split("/")[0]
        resp = MagicMock(ok=True)
        if method == "POST":
            resp.json.return_value = dict(self.keys.get(function, {}))
        elif method == "PUT":
            self.keys.setdefault(function, {})[registration.SITE24X7_KEY_NAME] = json["properties"]["value"]
        elif method == "DELETE":
            self.keys.get(function, {}).pop(registration.SITE24X7_KEY_NAME, None)
        return resp


def _with_arm(fake):
    cred = patch("azure.identity.DefaultAzureCredential")
    return cred, patch.object(registration.requests, "request", side_effect=fake.request)


def test_site24x7_gets_its_own_function_key_not_a_host_key():
    fake = _FakeArm({f: {"default": "customer-key"} for f in registration.SITE24X7_FUNCTIONS})
    cred, req = _with_arm(fake)
    with cred as c, req:
        c.return_value.get_token.return_value = MagicMock(token="arm-token")
        key = registration.get_function_key(SITE_ID)
    assert key and key != "customer-key"
    assert all(fake.keys[f][registration.SITE24X7_KEY_NAME] == key for f in registration.SITE24X7_FUNCTIONS)
    assert not any("/host/" in url for _, url in fake.calls)
    assert "Dashboard" not in registration.SITE24X7_FUNCTIONS and "GetDebugInfo" not in registration.SITE24X7_FUNCTIONS
    assert all(url.startswith(f"https://management.azure.com{SITE_ID}/functions/") for _, url in fake.calls)


def test_existing_uniform_key_is_reused_without_writes():
    fake = _FakeArm({f: {registration.SITE24X7_KEY_NAME: "s247-k"} for f in registration.SITE24X7_FUNCTIONS})
    cred, req = _with_arm(fake)
    with cred as c, req:
        c.return_value.get_token.return_value = MagicMock(token="t")
        assert registration.get_function_key(SITE_ID) == "s247-k"
    assert {m for m, _ in fake.calls} == {"POST"}


def test_key_rotated_on_one_function_is_reset_everywhere():
    keys = {f: {registration.SITE24X7_KEY_NAME: "old"} for f in registration.SITE24X7_FUNCTIONS}
    keys["TriggerScan"][registration.SITE24X7_KEY_NAME] = "rotated"
    fake = _FakeArm(keys)
    cred, req = _with_arm(fake)
    with cred as c, req:
        c.return_value.get_token.return_value = MagicMock(token="t")
        key = registration.get_function_key(SITE_ID)
    assert key not in ("old", "rotated")
    assert {fake.keys[f][registration.SITE24X7_KEY_NAME] for f in registration.SITE24X7_FUNCTIONS} == {key}


def test_excluded_functions_lose_the_site24x7_key(monkeypatch):
    monkeypatch.setenv("SITE24X7_KEY_EXCLUDE", "RemoveDiagSettings, checkupdate")
    fake = _FakeArm({f: {registration.SITE24X7_KEY_NAME: "s247-k"} for f in registration.SITE24X7_FUNCTIONS})
    cred, req = _with_arm(fake)
    with cred as c, req:
        c.return_value.get_token.return_value = MagicMock(token="t")
        registration.get_function_key(SITE_ID)
    # Revoked by overwriting (the resource group lock refuses deletes)
    assert fake.keys["RemoveDiagSettings"][registration.SITE24X7_KEY_NAME] != "s247-k"
    assert fake.keys["CheckUpdate"][registration.SITE24X7_KEY_NAME] != "s247-k"
    assert fake.keys["GetStatus"][registration.SITE24X7_KEY_NAME] == "s247-k"
    assert not any(m == "DELETE" for m, _ in fake.calls)


def test_excluding_everything_gives_site24x7_no_key(monkeypatch):
    monkeypatch.setenv("SITE24X7_KEY_EXCLUDE", "*")
    fake = _FakeArm({f: {registration.SITE24X7_KEY_NAME: "s247-k"} for f in registration.SITE24X7_FUNCTIONS})
    cred, req = _with_arm(fake)
    with cred as c, req:
        c.return_value.get_token.return_value = MagicMock(token="t")
        assert registration.get_function_key(SITE_ID) is None
    assert all(fake.keys[f][registration.SITE24X7_KEY_NAME] != "s247-k" for f in registration.SITE24X7_FUNCTIONS)


def test_keys_go_in_the_post_body_not_the_url(azure_env):
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"], captured["body"] = req.full_url, req.data.decode()
        resp = MagicMock()
        resp.__enter__.return_value.read.return_value = b'{"status":"SUCCESS"}'
        return resp

    with patch("shared.site24x7_client._KEYED_OPENER.open", side_effect=fake_urlopen):
        assert Site24x7Client().register_collector({"functionKey": SECRET, "resourceUrl": "u"})
    assert captured["url"].endswith("/applog/azure/register_collector")
    assert "?" not in captured["url"]
    form = urllib.parse.parse_qs(captured["body"])
    assert form["deviceKey"] == ["device-key-secret"]
    assert form["functionKey"] == [SECRET]


def test_failure_never_raises_or_logs_the_key(azure_env, no_state, caplog):
    caplog.set_level(logging.DEBUG)
    with patch.object(registration, "get_function_key", return_value=SECRET), \
         patch("shared.site24x7_client._KEYED_OPENER.open", side_effect=OSError(f"boom {SECRET}")):
        assert registration.register_with_site24x7() is False
    assert SECRET not in caplog.text



def test_registration_uses_deployment_identity_when_monitored_subscriptions_differ(azure_env, no_state, monkeypatch):
    app_id = "/subscriptions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/resourceGroups/collector-rg/providers/Microsoft.Web/sites/s247-diag-func-oct14"
    monkeypatch.setenv("COLLECTOR_RESOURCE_ID", app_id)
    monkeypatch.setenv("SUBSCRIPTION_IDS", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    with patch.object(registration, "get_function_key", return_value=SECRET) as read_key, \
         patch("shared.config_store.get_configured_resources", return_value={}):
        fields = registration.build_registration()
    read_key.assert_called_once_with(app_id)
    assert fields["subscriptionId"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert fields["resourceGroup"] == "collector-rg"
    assert json.loads(fields["status"])["subscription_ids"] == ["bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"]


def test_dashboard_summary_excludes_invalid_subscription_ids_and_preserves_counts(azure_env, no_state, monkeypatch):
    valid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    monkeypatch.setenv("SUBSCRIPTION_IDS", f"{valid},invalid-secret,{valid.upper()}")
    with patch("shared.config_store.get_configured_resources", return_value={"resource-1": {}, "resource-2": {}}):
        summary = registration.build_status_summary()
    assert summary["subscription_ids"] == [valid]
    assert summary["configured_resources"] == 2
    assert summary["subscriptions_truncated"] is False
    assert "invalid-secret" not in json.dumps(summary)


def test_unknown_configured_count_does_not_prevent_registration(azure_env, no_state):
    with patch("shared.config_store.get_configured_resources", side_effect=OSError("storage unavailable")):
        summary = registration.build_status_summary()
    assert "configured_resources" not in summary
    assert summary["active_resources"] == 152


@pytest.mark.parametrize("given", [
    "/subscriptions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/resourcegroups/collector-rg/providers/microsoft.web/sites/s247-diag-func-oct14",
    "/SUBSCRIPTIONS/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/RESOURCEGROUPS/collector-rg/PROVIDERS/MICROSOFT.WEB/SITES/s247-diag-func-oct14",
])
def test_collector_resource_id_is_sent_in_canonical_case(azure_env, no_state, monkeypatch, given):
    # Site24x7 validates resourceGroups / Microsoft.Web/sites case-sensitively.
    monkeypatch.setenv("COLLECTOR_RESOURCE_ID", given)
    with patch.object(registration, "get_function_key", return_value=SECRET) as read_key, \
         patch("shared.config_store.get_configured_resources", return_value={}):
        fields = registration.build_registration()
    canonical = ("/subscriptions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/resourceGroups/collector-rg"
                 "/providers/Microsoft.Web/sites/s247-diag-func-oct14")
    read_key.assert_called_once_with(canonical)
    assert fields["resourceId"] == canonical
    assert fields["resourceGroup"] == "collector-rg"


def test_invalid_collector_resource_id_falls_back_to_app_environment(azure_env, no_state, monkeypatch):
    monkeypatch.setenv("COLLECTOR_RESOURCE_ID", "/subscriptions/not-a-guid/resourceGroups/x/providers/Microsoft.Web/sites/y")
    with patch.object(registration, "get_function_key", return_value=SECRET), \
         patch("shared.config_store.get_configured_resources", return_value={}):
        fields = registration.build_registration()
    assert fields["resourceId"] == SITE_ID


SUB_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUB_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def test_summary_carries_subscription_names_and_the_recent_window(azure_env, no_state, monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_IDS", f"{SUB_A},{SUB_B}")
    with patch.object(registration, "_subscription_names", return_value={SUB_A: "Production"}), \
         patch("shared.config_store.get_configured_resources", return_value={}):
        summary = registration.build_status_summary()
    assert summary["subscription_names"] == {SUB_A: "Production"}
    # The recent counts cover the stored runs; the oldest run says since when.
    assert summary["recent_since"] == "2026-10-14T11:30:00+00:00"
    assert summary["recent_uploaded"] == 10


def test_subscription_names_stop_before_the_status_limit(azure_env, no_state, monkeypatch):
    ids = [f"{i:08x}-aaaa-aaaa-aaaa-aaaaaaaaaaaa" for i in range(50)]
    monkeypatch.setenv("SUBSCRIPTION_IDS", ",".join(ids))
    with patch.object(registration, "_subscription_names", return_value={i: "n" * 64 for i in ids}), \
         patch("shared.config_store.get_configured_resources", return_value={}):
        summary = registration.build_status_summary()
    assert 0 < len(summary["subscription_names"]) < 50
    assert len(json.dumps(summary, separators=(",", ":"))) <= registration._STATUS_BUDGET


def test_subscription_names_need_a_managed_identity(monkeypatch):
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("MSI_ENDPOINT", raising=False)
    assert registration._subscription_names([SUB_A]) == {}


def test_subscription_names_keep_only_monitored_ones_as_plain_text(monkeypatch):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost/identity")
    listed = [MagicMock(subscription_id=SUB_A.upper(), display_name=" Prod\x07uction "),
              MagicMock(subscription_id=SUB_B, display_name="Not monitored")]
    client = MagicMock()
    client.return_value.subscriptions.list.return_value = listed
    with patch("azure.identity.DefaultAzureCredential"), patch("azure.mgmt.resource.SubscriptionClient", client):
        assert registration._subscription_names([SUB_A]) == {SUB_A: "Production"}


def test_subscription_name_failure_falls_back_to_ids(monkeypatch):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://localhost/identity")
    with patch("azure.identity.DefaultAzureCredential", side_effect=RuntimeError("no identity")):
        assert registration._subscription_names([SUB_A]) == {}


def test_registration_passes_its_timeout_to_site24x7(azure_env, no_state):
    with patch.object(registration, "get_function_key", return_value=SECRET), \
         patch("shared.site24x7_client.Site24x7Client.register_collector", return_value=True) as call:
        assert registration.register_with_site24x7(timeout=10) is True
    assert call.call_args.kwargs["timeout"] == 10


class TestSyncRegistration:
    """Register after a restart and when the function key changes, without slowing every run."""

    @pytest.fixture(autouse=True)
    def fresh_worker(self, monkeypatch):
        monkeypatch.setattr(registration, "_registered_key_hash", None)
        monkeypatch.setattr(registration, "_last_key_check", 0.0)
        self.now = [1000.0]
        monkeypatch.setattr(registration.time, "monotonic", lambda: self.now[0])

    def _register_ok(self, key):
        def register(timeout=60, reuse_identity=False):
            registration._registered_key_hash = registration._key_hash(key)
            return True
        return register

    def test_first_run_of_a_worker_registers(self):
        with patch.object(registration, "register_with_site24x7", side_effect=self._register_ok("k1")) as reg:
            assert registration.sync_registration() is True
        reg.assert_called_once()

    def test_unchanged_key_is_not_registered_again(self):
        with patch.object(registration, "register_with_site24x7", side_effect=self._register_ok("k1")) as reg, \
             patch.object(registration, "_site_resource_id", return_value=SITE_ID), \
             patch.object(registration, "get_function_key", return_value="k1") as key:
            registration.sync_registration()
            self.now[0] += 60
            assert registration.sync_registration() is False      # within the interval: no ARM read
            assert key.call_count == 0
            self.now[0] += registration.KEY_CHECK_INTERVAL_SEC
            assert registration.sync_registration() is False      # key read, same key
        assert reg.call_count == 1 and key.call_count == 1

    def test_rotated_key_is_registered(self):
        with patch.object(registration, "register_with_site24x7", side_effect=self._register_ok("k1")) as reg, \
             patch.object(registration, "_site_resource_id", return_value=SITE_ID), \
             patch.object(registration, "get_function_key", return_value="k2"):
            registration.sync_registration()
            self.now[0] += registration.KEY_CHECK_INTERVAL_SEC
            registration.sync_registration()
        assert reg.call_count == 2

    def test_failed_registration_waits_before_retrying(self):
        with patch.object(registration, "register_with_site24x7", return_value=False) as reg:
            registration.sync_registration()
            self.now[0] += 120
            registration.sync_registration()                      # next processing run: no retry yet
            self.now[0] += registration.KEY_CHECK_INTERVAL_SEC
            registration.sync_registration()
        assert reg.call_count == 2


def test_summary_reports_skipped_parts_and_a_refused_device_key(azure_env, no_state):
    runs = [{"timestamp": "2026-10-23T00:02:00+00:00", "uploaded": 0, "dropped": 3, "retrying": 5,
             "skipped_parts": 1, "upload_refused": True},
            {"timestamp": "2026-10-23T00:00:00+00:00", "uploaded": 7, "dropped": 0, "skipped_parts": 2}]
    with patch("shared.debug_logger.get_processing_stats", return_value=runs), \
         patch("shared.config_store.get_configured_resources", return_value={}):
        summary = registration.build_status_summary()
    assert summary["recent_skipped_parts"] == 3
    assert summary["recent_dropped"] == 3              # records only, oversized parts apart
    assert summary["upload_refused"] is True           # from the latest run
    assert summary["pending_records"] == 5


def test_operational_status_preserves_failure_across_empty_runs(monkeypatch):
    runs = [{"timestamp": "2026-10-23T08:02:00Z", "uploaded": 0},
            {"timestamp": "2026-10-23T08:00:00Z", "upload_status": "failed", "upload_http_status": 504}]
    with patch("shared.config_store.get_scan_state", return_value={"s247_reachable": False, "s247_errors": [
             {"phase": "catalog", "http_status": 504, "message": "key=secret"},
             {"phase": "evil", "http_status": 200}]}), \
         patch("shared.config_store.get_configured_resources", return_value={}), \
         patch("shared.debug_logger.get_processing_stats", return_value=runs):
        result = registration.build_status_summary(include_names=False)
    assert result["s247_errors"] == [{"phase": "catalog", "http_status": 504}]
    assert result["upload_status"] == "failed" and result["upload_http_status"] == 504
    assert result["upload_status_time"] == "2026-10-23T08:00:00Z"
    assert "secret" not in json.dumps(result)


def test_operational_status_new_success_clears_previous_upload_failure():
    runs = [{"timestamp": "2026-10-23T08:04:00Z", "upload_status": "ok", "upload_http_status": 200},
            {"timestamp": "2026-10-23T08:00:00Z", "upload_status": "failed", "upload_http_status": 504}]
    with patch("shared.config_store.get_scan_state", return_value={}), \
         patch("shared.config_store.get_configured_resources", return_value={}), \
         patch("shared.debug_logger.get_processing_stats", return_value=runs):
        result = registration.build_status_summary(include_names=False)
    assert result["upload_status"] == "ok" and result["upload_http_status"] == 200
