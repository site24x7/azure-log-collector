"""Tests for shared/log_parser.py — pure logic, no mocks needed."""

from shared.log_parser import parse_diagnostic_records, extract_resource_info


# ─── parse_diagnostic_records ────────────────────────────────────────────────


class TestParseDiagnosticRecords:
    def test_valid_envelope(self):
        body = '{"records": [{"time": "2024-01-01T00:00:00Z", "resourceId": "/sub/123", "category": "AuditEvent", "operationName": "Write", "resultType": "Success", "level": "Informational", "properties": {"key": "val"}}]}'
        result = parse_diagnostic_records(body)
        assert len(result) == 1
        r = result[0]
        assert r["time"] == "2024-01-01T00:00:00Z"
        assert r["resource_id"] == "/sub/123"
        assert r["category"] == "AuditEvent"
        assert r["operation_name"] == "Write"
        assert r["result_type"] == "Success"
        assert r["level"] == "Informational"
        assert r["properties"] == {"key": "val"}
        assert r["raw"]["time"] == "2024-01-01T00:00:00Z"

    def test_multiple_records(self):
        body = '{"records": [{"time": "t1"}, {"time": "t2"}, {"time": "t3"}]}'
        result = parse_diagnostic_records(body)
        assert len(result) == 3
        assert [r["time"] for r in result] == ["t1", "t2", "t3"]

    def test_dict_input(self):
        data = {"records": [{"time": "t1", "category": "Cat1"}]}
        result = parse_diagnostic_records(data)
        assert len(result) == 1
        assert result[0]["category"] == "Cat1"

    def test_missing_records_field(self):
        result = parse_diagnostic_records('{"other": "data"}')
        assert result == []

    def test_empty_records(self):
        result = parse_diagnostic_records('{"records": []}')
        assert result == []

    def test_invalid_json(self):
        result = parse_diagnostic_records("not json at all")
        assert result == []

    def test_none_input(self):
        result = parse_diagnostic_records(None)
        assert result == []

    def test_missing_fields_get_defaults(self):
        body = '{"records": [{}]}'
        result = parse_diagnostic_records(body)
        assert len(result) == 1
        r = result[0]
        assert r["time"] == ""
        assert r["resource_id"] == ""
        assert r["category"] == ""
        assert r["operation_name"] == ""
        assert r["result_type"] == ""
        assert r["level"] == ""
        assert r["properties"] == {}


# ─── extract_resource_info ───────────────────────────────────────────────────


class TestExtractResourceInfo:
    def test_full_resource_id(self):
        rid = "/subscriptions/abc-123/resourceGroups/my-rg/providers/Microsoft.Compute/virtualMachines/my-vm"
        info = extract_resource_info(rid)
        assert info["subscription_id"] == "abc-123"
        assert info["resource_group"] == "my-rg"
        assert info["provider"] == "Microsoft.Compute"
        assert info["resource_type"] == "virtualMachines"
        assert info["resource_name"] == "my-vm"

    def test_no_leading_slash(self):
        rid = "subscriptions/abc/resourceGroups/rg1/providers/Microsoft.Network/loadBalancers/lb1"
        info = extract_resource_info(rid)
        assert info["subscription_id"] == "abc"
        assert info["resource_group"] == "rg1"
        assert info["provider"] == "Microsoft.Network"
        assert info["resource_type"] == "loadBalancers"
        assert info["resource_name"] == "lb1"

    def test_case_insensitive_keywords(self):
        rid = "/Subscriptions/sub1/ResourceGroups/rg1/Providers/Microsoft.Storage/storageAccounts/sa1"
        info = extract_resource_info(rid)
        assert info["subscription_id"] == "sub1"
        assert info["resource_group"] == "rg1"
        assert info["provider"] == "Microsoft.Storage"

    def test_subscription_only(self):
        rid = "/subscriptions/sub-only"
        info = extract_resource_info(rid)
        assert info["subscription_id"] == "sub-only"
        assert info["resource_group"] == ""
        assert info["provider"] == ""

    def test_empty_string(self):
        info = extract_resource_info("")
        assert info["subscription_id"] == ""
        assert info["resource_group"] == ""
        assert info["provider"] == ""
        assert info["resource_type"] == ""
        assert info["resource_name"] == ""

    def test_provider_without_resource_name(self):
        rid = "/subscriptions/s/resourceGroups/g/providers/Microsoft.Web/sites"
        info = extract_resource_info(rid)
        assert info["provider"] == "Microsoft.Web"
        assert info["resource_type"] == "sites"
        assert info["resource_name"] == ""
