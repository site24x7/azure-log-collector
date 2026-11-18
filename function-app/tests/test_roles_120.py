"""Deployment and migration contracts for the F-04 collector roles."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[2] / "setup"
CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
STORAGE = "17d1049b-9a84-46fb-8f53-869881c3d3ab"
WEBSITE = "de139f84-1756-47ae-9be6-808fbbe84772"
LOCKS = "28bf596f-4eb7-45ce-b5bc-6cf482fec137"
RG_DELETE = "Site24x7 Collector RG Delete v1"
SITE_ID = "[resourceId('Microsoft.Web/sites', parameters('functionAppName'))]"
ROLE_DEF_TYPE = "Microsoft.Authorization/roleDefinitions"
ROLE_ASSIGNMENT_TYPE = "Microsoft.Authorization/roleAssignments"


@pytest.fixture(scope="module")
def template():
    return json.loads((SETUP / "azuredeploy.json").read_text())


def resources(template):
    for resource in template["resources"]:
        yield resource
        nested = resource.get("properties", {}).get("template")
        if nested is not None:
            yield from resources(nested)


def test_collectors_replace_rg_contributor_with_scoped_roles(template):
    assignments = [r for r in resources(template) if r["type"] == ROLE_ASSIGNMENT_TYPE]
    assert all(CONTRIBUTOR not in r["properties"]["roleDefinitionId"] for r in assignments)
    assert all(r["properties"]["principalType"] == "ServicePrincipal" for r in assignments)
    for guid, scope in ((STORAGE, None), (WEBSITE, SITE_ID), (LOCKS, None)):
        assignment, = [r for r in assignments if guid in r["properties"]["roleDefinitionId"]]
        assert assignment.get("scope") == scope
        if guid != LOCKS:
            scope_expression = scope[1:-1] if scope else "resourceGroup().id"
            role_definition = assignment["properties"]["roleDefinitionId"][1:-1]
            assert assignment["name"] == f"[guid({scope_expression}, parameters('principalId'), {role_definition})]"

    custom_assignment, = [r for r in assignments if r.get("comments") == RG_DELETE]
    assert "scope" not in custom_assignment
    assert custom_assignment["name"] == (
        "[guid(resourceGroup().id, parameters('principalId'), parameters('rgDeleteRoleDefinitionId'))]"
    )
    assert custom_assignment["properties"]["roleDefinitionId"] == "[parameters('rgDeleteRoleDefinitionId')]"
    for role in ("readerRoleId", "monitoringContributorRoleId"):
        assert any(role in r["properties"]["roleDefinitionId"] for r in assignments)


def test_custom_role_is_defined_at_the_hosting_subscription(template):
    custom, = [r for r in template["resources"] if r["type"] == ROLE_DEF_TYPE]
    assert custom["name"] == "[guid(subscription().id, 's247-collector-rg-delete-v1')]"
    assert custom["properties"]["type"] == "CustomRole"
    assert custom["properties"]["roleName"] == "[concat('" + RG_DELETE + " (', subscription().subscriptionId, ')')]"
    assert custom["properties"]["assignableScopes"] == ["[subscription().id]"]
    assert custom["properties"]["permissions"] == [{
        "actions": [
            "Microsoft.Resources/subscriptions/resourceGroups/delete",
            "Microsoft.Resources/subscriptions/resourceGroups/read",
        ],
        "notActions": [],
        "dataActions": [],
        "notDataActions": [],
    }]
    collector, = [r for r in template["resources"] if r["name"] == "s247-diag-logs-resources"]
    definition_id = collector["properties"]["parameters"]["rgDeleteRoleDefinitionId"]["value"]
    assert definition_id == f"[subscriptionResourceId('{ROLE_DEF_TYPE}', {custom['name'][1:-1]})]"
    assert definition_id in collector["dependsOn"]
    scoped, = [
        r for r in collector["properties"]["template"]["resources"]
        if r["name"] == "[concat(parameters('functionAppName'), '-scoped-roles')]"
    ]
    assert SITE_ID in scoped["dependsOn"]
    assert ").identity.principalId]" in scoped["properties"]["parameters"]["principalId"]["value"]
    assert scoped["properties"]["parameters"]["rgDeleteRoleDefinitionId"]["value"] == (
        "[parameters('rgDeleteRoleDefinitionId')]"
    )


def test_shell_installer_mentions_the_same_roles():
    setup = (SETUP / "setup.sh").read_text()
    assert CONTRIBUTOR not in setup
    for value in (STORAGE, WEBSITE, LOCKS, RG_DELETE, "Reader", "Monitoring Contributor"):
        assert value in setup


@pytest.mark.parametrize("script", ["setup.sh", "teardown.sh", "migrate-roles.sh"])
def test_role_scripts_parse(script):
    subprocess.run(["bash", "-n", str(SETUP / script)], check=True)


@pytest.mark.parametrize("mode", ["plan", "apply", "timeout"])
def test_migration_only_deletes_exact_contributor_after_grants_appear(tmp_path, mode):
    if not shutil.which("jq"):
        pytest.skip("Migration requires jq")

    subscription = "11111111-1111-1111-1111-111111111111"
    principal = "22222222-2222-2222-2222-222222222222"
    sub_scope = f"/subscriptions/{subscription}"
    group = f"{sub_scope}/resourceGroups/collector-rg"
    site = f"{group}/providers/Microsoft.Web/sites/collector-app"
    provider = "Microsoft.Authorization"
    custom = "33333333-3333-3333-3333-333333333333"

    def assignment(scope, role, guid, assignee=principal):
        return {
            "id": f"{scope}/providers/{provider}/roleAssignments/{guid}",
            "scope": scope,
            "roleDefinitionId": f"{sub_scope}/providers/{provider}/roleDefinitions/{role}",
            "principalId": assignee,
        }

    legacy = assignment(group, CONTRIBUTOR, "44444444-4444-4444-4444-444444444444")
    inherited = assignment(sub_scope, CONTRIBUTOR, "55555555-5555-5555-5555-555555555555")
    other_principal = assignment(group, CONTRIBUTOR, "66666666-6666-6666-6666-666666666666", subscription)
    replacement = [
        assignment(scope, role, "77777777-7777-7777-7777-777777777777")
        for scope, role in ((group, STORAGE), (site, WEBSITE), (group, LOCKS), (group, custom))
    ]
    definition = {
        "id": replacement[-1]["roleDefinitionId"],
        "assignableScopes": [sub_scope],
        "permissions": [{
            "actions": [
                "Microsoft.Resources/subscriptions/resourceGroups/read",
                "Microsoft.Resources/subscriptions/resourceGroups/delete",
            ]
        }],
    }
    calls = tmp_path / "calls.log"
    env = dict(
        os.environ,
        SUBSCRIPTION=subscription,
        GROUP=group,
        SITE=json.dumps({"id": site, "principal": principal}),
        PRINCIPAL=principal,
        BEFORE=json.dumps([legacy, inherited, other_principal]),
        AFTER=json.dumps([legacy, inherited, other_principal, *replacement]),
        DEFINITION=json.dumps([definition]),
        MODE=mode,
        CALLS=str(calls),
    )
    runner = r'''
source "$1"
shift
sleep() { :; }
az() {
    printf '%s\n' "$*" >> "$CALLS"
    case "$1 $2 ${3:-}" in
        'account show --query') printf '%s\n' "$SUBSCRIPTION" ;;
        'group show --subscription') printf '%s\n' "$GROUP" ;;
        'functionapp show --subscription')
            if [[ "$*" == *'--query identity.principalId'* ]]; then
                printf '%s\n' "$PRINCIPAL"
            else
                printf '%s\n' "$SITE"
            fi ;;
        'role definition list') printf '%s\n' "$DEFINITION" ;;
        'role assignment list')
            if [[ "$MODE" == apply ]] && grep -q '^role assignment create ' "$CALLS"; then
                printf '%s\n' "$AFTER"
            else
                printf '%s\n' "$BEFORE"
            fi ;;
        'role assignment create'|'role assignment delete') : ;;
        *) return 1 ;;
    esac
}
migrate_collector_roles "$@"
'''
    flags = ["--apply"] if mode != "plan" else []
    result = subprocess.run(
        ["bash", "-c", runner, "roles-test", str(SETUP / "migrate-roles.sh"),
         "--app", "collector-app", "--rg", "collector-rg", *flags],
        env=env, cwd=tmp_path, capture_output=True, text=True,
    )
    commands = calls.read_text().splitlines()
    creates = [call for call in commands if call.startswith("role assignment create ")]
    deletes = [call for call in commands if call.startswith("role assignment delete ")]
    if mode == "plan":
        assert result.returncode == 0, result.stderr
        assert "PLAN only" in result.stdout
        assert inherited["id"] in result.stdout
        assert not creates and not deletes
        assert not list(tmp_path.glob(".s247-role-migration-*.json"))
    elif mode == "apply":
        assert result.returncode == 0, result.stderr
        assert len(creates) == 4
        assert len(deletes) == 1
        assert f"--ids {legacy['id']} " in deletes[0]
        assert commands.index(deletes[0]) > commands.index(creates[-1])
        assert "Administrator rollback command" in result.stdout
        assert legacy["id"].split("/")[-1] in result.stdout
        manifest = json.loads((tmp_path / f".s247-role-migration-{principal}.json").read_text())
        assert manifest["originalAssignment"] == legacy["id"]
        assert manifest["phase"] == "assignments-updated"
    else:
        assert result.returncode != 0
        assert "Timed out" in result.stderr
        assert len(creates) == 4
        assert not deletes
