"""Records of a multi-category container (Azure Firewall) go to their own log type, and
records without "time" (Application Gateway firewall log) take "timeStamp"."""

import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from BlobLogProcessor import _record_router, _upload_routed, _process_all_regions
from shared.site24x7_client import _json_log_parser
from tests.test_blob_log_processor import _FakeContainerClient, _build_blob, proc_env  # noqa: F401

NAT = {"time": "2026-11-24T08:48:04.841708+00:00", "category": "AZFWNatRule", "properties": {"Rule": "ssh-in"}}
DNS = {"time": "2026-11-24T08:44:45.127919+00:00", "category": "AZFWDnsQuery", "properties": {"QueryName": "x."}}
FLOW = {"time": "2026-11-24T08:44:45.127919+00:00", "category": "AZFWFatFlow", "properties": {}}
CONFIGS = {"S247_azfwnatrule": {"logType": "nat"}, "S247_azfwdnsquery": {"logType": "dns"}}


def _b64(config):
    return base64.b64encode(json.dumps(config).encode()).decode()


def _stats():
    return {"uploaded": 0, "general": 0, "dropped": 0, "retrying": 0, "rejected": 0,
            "batches_sent": 0, "error_blobs": [], "upload_refused": False}


def _client(results=None):
    client = MagicMock()
    outcomes = iter(results or [])
    client.post_logs.side_effect = lambda cfg, batch: next(outcomes, True)
    client.last_unparsed = 0
    client.last_rejected = 0
    client.upload_refused = False
    return client


def test_each_record_goes_to_its_own_log_type_in_order():
    router = _record_router(CONFIGS, False, "", lambda c: False)
    client, stats = _client(), _stats()
    assert _upload_routed(router, client, None, [NAT, NAT, DNS, NAT], False,
                          stats, "a", "c", "b") == 4
    calls = [(json.loads(base64.b64decode(c.args[0]))["logType"], len(c.args[1]))
             for c in client.post_logs.call_args_list]
    assert calls == [("nat", 2), ("dns", 1), ("nat", 1)]
    assert stats["uploaded"] == 4 and stats["retrying"] == 0


def test_a_failed_run_stops_the_later_ones():
    router = _record_router(CONFIGS, False, "", lambda c: False)
    client, stats = _client([True, False]), _stats()
    assert _upload_routed(router, client, None, [NAT, DNS, DNS, NAT], False,
                          stats, "a", "c", "b") == 1
    assert client.post_logs.call_count == 2
    assert stats["retrying"] == 3


def test_switched_off_and_unknown_types_are_not_sent():
    router = _record_router(CONFIGS, False, "", lambda c: c == "azfwdnsquery")
    client, stats = _client(), _stats()
    assert _upload_routed(router, client, None, [DNS, FLOW, NAT], False,
                          stats, "a", "c", "b") == 3
    assert [len(c.args[1]) for c in client.post_logs.call_args_list] == [1]
    assert stats["dropped"] == 1          # FLOW: no log type; DNS is switched off, not dropped


def test_unknown_types_use_the_general_type_when_it_is_on():
    router = _record_router(CONFIGS, True, "general", lambda c: False)
    client, stats = _client(), _stats()
    _upload_routed(router, client, None, [FLOW], False, stats, "a", "c", "b")
    assert client.post_logs.call_args.args[0] == "general"
    assert stats["general"] == 1 and stats["dropped"] == 0


def test_without_a_router_the_container_config_is_used():
    client, stats = _client(), _stats()
    _upload_routed(None, client, "cfg", [NAT, DNS], False, stats, "a", "c", "b")
    assert [c.args[0] for c in client.post_logs.call_args_list] == ["cfg"]


def test_the_firewall_container_is_read_without_a_container_config(proc_env):
    body = (json.dumps({"records": [NAT, DNS]}) + "\n").encode()
    _, _, fbc = _build_blob([body])
    fbc.blob_type = "AppendBlob"
    service = MagicMock()
    service.list_containers.return_value = [{"name": "insights-logs-azurefirewall"}]
    service.get_container_client.return_value = _FakeContainerClient(
        [("PT1H.json", fbc, datetime(2026, 11, 24, tzinfo=timezone.utc))])
    sa = SimpleNamespace(name="s247diagtest", primary_location="eastus",
                         tags={"managed-by": "s247-diag-logs", "purpose": "diag-logs-regional"})
    mgmt = MagicMock()
    mgmt.storage_accounts.list_by_resource_group.return_value = [sa]
    mgmt.storage_accounts.list_keys.return_value = SimpleNamespace(keys=[SimpleNamespace(value="key")])
    client = _client()
    with patch("shared.site24x7_client.Site24x7Client", return_value=client), \
         patch("BlobLogProcessor.DefaultAzureCredential"), \
         patch("BlobLogProcessor.StorageManagementClient", return_value=mgmt), \
         patch("BlobLogProcessor.BlobServiceClient.from_connection_string", return_value=service), \
         patch("BlobLogProcessor._save_checkpoints"), \
         patch("BlobLogProcessor._load_checkpoints", return_value={}), \
         patch("shared.config_store.get_all_logtype_configs", return_value=CONFIGS), \
         patch("shared.config_store.clear_cache"):
        _process_all_regions()
    sent = [(c.args[0], c.args[1]) for c in client.post_logs.call_args_list]
    assert sent == [(_b64(CONFIGS["S247_azfwnatrule"]), [NAT]),
                    (_b64(CONFIGS["S247_azfwdnsquery"]), [DNS])]


def test_timestamp_fills_time_when_the_record_has_no_time():
    event = {"timeStamp": "2026-11-24T08:19:00+00:00", "category": "ApplicationGatewayFirewallLog"}
    config = {"dateField": "Time", "jsonPath": [{"name": "Time", "key": "time", "type": "date"}]}
    lines, _ = _json_log_parser([event], config, None, None, None)
    assert lines[0]["Time"] == "2026-11-24T08:19:00+00:00"
    assert lines[0]["_zl_timestamp"] == 1795508340000


def test_timestamp_is_used_for_the_log_time_without_a_time_field():
    event = {"timeStamp": "2026-11-24T08:19:00+00:00"}
    lines, _ = _json_log_parser([event], {"dateField": "time", "jsonPath": []}, None, None, None)
    assert lines[0]["_zl_timestamp"] == 1795508340000


def test_time_wins_over_timestamp():
    event = {"time": "2026-11-24T08:19:01+00:00", "timeStamp": "2026-11-24T08:19:00+00:00"}
    config = {"dateField": "Time", "jsonPath": [{"name": "Time", "key": "time"}]}
    lines, _ = _json_log_parser([event], config, None, None, None)
    assert lines[0]["Time"] == "2026-11-24T08:19:01+00:00"
