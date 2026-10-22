"""Regression checks for global-name collisions and deployment compatibility."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess

import pytest

from shared.region_manager import _storage_account_name, _tenant_storage_name
from shared.storage_names import regional_storage_name, validate_suffix

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize('region', ['eastus', 'centralindia', 'australiasoutheast'])
def test_legacy_region_names_unchanged(region):
    # Names that fit are unchanged; earlier versions' cut-off names stay recognised.
    from shared.storage_names import legacy_regional_storage_name
    assert legacy_regional_storage_name(region, 'qt4xn2') == f's247diag{region}qt4xn2'[:24]
    if len(f's247diag{region}qt4xn2') <= 24:
        assert _storage_account_name(region, 'qt4xn2') == f's247diag{region}qt4xn2'
    assert _tenant_storage_name('qt4xn2') == 's247diagtenantqt4xn2'


def test_short_suffix_is_never_cut_off_in_long_regions():
    names = {_storage_account_name('switzerlandnorth', s) for s in ('alpha', 'beta')}
    assert len(names) == 2 and all(len(n) <= 24 for n in names)


def test_long_suffix_keeps_distinct_regions_and_full_suffix(monkeypatch):
    monkeypatch.setenv('DIAG_SEED_REGION', 'centralindia')
    suffix = 'c9a6f201a4d9'
    names = {_storage_account_name(region, suffix) for region in
             ['centralindia', 'australiasoutheast', 'australiacentral', 'australiacentral2', 'southcentralus']}
    assert len(names) == 5
    assert 's247drc9a6f201a4d9' in names
    assert _tenant_storage_name(suffix) == 's247dtc9a6f201a4d9'
    for name in names:
        assert re.fullmatch('[a-z0-9]{3,24}', name)
    # Previously truncating the end could erase this changed character.
    assert _storage_account_name('australiasoutheast', suffix) != _storage_account_name('australiasoutheast', suffix[:-1] + '8')
    assert _storage_account_name('southcentralus', suffix) == _storage_account_name('South Central US', suffix)


@pytest.mark.parametrize('suffix', ['', 'ABC123', 'bad-name', 'a' * 14, '../bad'])
def test_invalid_suffix_rejected(suffix):
    with pytest.raises(ValueError):
        validate_suffix(suffix)


def load_preflight():
    spec = importlib.util.spec_from_file_location('storage_preflight', ROOT / 'setup/check-storage-names.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('scenario', ['free', 'owned', 'taken', 'unowned', 'wrong_region', 'outage', 'unknown'])
def test_preflight_checks_availability_and_ownership(scenario):
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if 'check-name' in cmd:
            if scenario == 'outage':
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'nameAvailable':
                True if scenario == 'free' else None if scenario == 'unknown' else False}))
        if scenario == 'taken':
            return subprocess.CompletedProcess(cmd, 1, '')
        return subprocess.CompletedProcess(cmd, 0, json.dumps({
            'primaryLocation': 'eastus' if scenario == 'wrong_region' else 'centralindia',
            'tags': {} if scenario == 'unowned' else {'managed-by': 's247-diag-logs'}}))
    check = load_preflight().check_names
    if scenario in ('free', 'owned'):
        check('c9a6f201a4d9', 'centralindia', 's247-diag-logs-rg', 'demo-sub', run)
        checked = [cmd[cmd.index('--name') + 1] for cmd in calls if 'check-name' in cmd]
        assert checked == ['s247diagc9a6f201a4d9', 's247drc9a6f201a4d9', 's247dtc9a6f201a4d9']
    else:
        with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
            check('c9a6f201a4d9', 'centralindia', 's247-diag-logs-rg', 'demo-sub', run)
    assert all('--subscription' in cmd for cmd in calls)
    assert all('create' not in cmd and 'delete' not in cmd for cmd in calls)


def test_template_strips_separators_from_deployment_suffix():
    # Storage account names allow only [a-z0-9]; a portal suffix like "-Oct14"
    # must become "oct14" for every derived name, including DIAG_STORAGE_SUFFIX.
    variables = json.loads((ROOT / 'setup/azuredeploy.json').read_text())['variables']
    clean = variables['cleanSuffix']
    assert "toLower(trim(parameters('deploymentSuffix')))" in clean
    for separator in ("'-'", "'_'", "'.'", "' '"):
        assert f"{separator}, '')" in clean
    assert variables['uniqueSuffix'].startswith("[if(empty(variables('cleanSuffix'))")
    assert variables['uniqueSuffix'].endswith(", variables('cleanSuffix'))]")
    assert "parameters('deploymentSuffix')" not in variables['uniqueSuffix']


# Settings the Flex Consumption plan rejects or ignores (Microsoft: "Flex
# Consumption plan deprecations"), plus the retired instrumentation key.
FLEX_DEPRECATED_SETTINGS = {
    'ENABLE_ORYX_BUILD', 'FUNCTIONS_EXTENSION_VERSION', 'FUNCTIONS_WORKER_RUNTIME',
    'FUNCTIONS_WORKER_RUNTIME_VERSION', 'FUNCTIONS_MAX_HTTP_CONCURRENCY', 'FUNCTIONS_WORKER_PROCESS_COUNT',
    'FUNCTIONS_WORKER_DYNAMIC_CONCURRENCY_ENABLED', 'SCM_DO_BUILD_DURING_DEPLOYMENT',
    'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING', 'WEBSITE_CONTENTOVERVNET', 'WEBSITE_CONTENTSHARE',
    'WEBSITE_DNS_SERVER', 'WEBSITE_MAX_DYNAMIC_APPLICATION_SCALE_OUT', 'WEBSITE_NODE_DEFAULT_VERSION',
    'WEBSITE_RUN_FROM_PACKAGE', 'WEBSITE_SKIP_CONTENTSHARE_VALIDATION', 'WEBSITE_VNET_ROUTE_ALL',
    'WEBSITE_TIME_ZONE', 'TZ', 'APPINSIGHTS_INSTRUMENTATIONKEY',
}


def _nested_resources():
    template = json.loads((ROOT / 'setup/azuredeploy.json').read_text())
    inner = next(r for r in template['resources'] if r['name'] == 's247-diag-logs-resources')
    return template, inner['properties']['template']['resources']


def test_template_runs_on_flex_consumption():
    template, nested = _nested_resources()
    plan = next(r for r in nested if r['type'] == 'Microsoft.Web/serverfarms')
    assert plan['sku'] == {'name': 'FC1', 'tier': 'FlexConsumption'} and plan['properties']['reserved'] is True
    site = next(r for r in nested if r['type'] == 'Microsoft.Web/sites')
    config = site['properties']['functionAppConfig']
    assert config['runtime'] == {'name': 'python', 'version': '3.13'}
    assert 'linuxFxVersion' not in site['properties']['siteConfig']
    settings = {s['name']: s['value'] for s in site['properties']['siteConfig']['appSettings']}
    assert not FLEX_DEPRECATED_SETTINGS & settings.keys()
    assert 'APPLICATIONINSIGHTS_CONNECTION_STRING' in settings
    assert settings['DIAG_SEED_REGION'] == "[parameters('location')]"
    # The deployment container exists before the app, and its connection setting is defined.
    storage = config['deployment']['storage']
    assert storage['type'] == 'blobContainer' and "parameters('deploymentContainerName')" in storage['value']
    assert storage['authentication']['storageAccountConnectionStringName'] in settings
    container = next(r for r in nested if r['type'] == 'Microsoft.Storage/storageAccounts/blobServices/containers'
                     and 'deploymentContainerName' in r['name'])
    assert any('deploymentContainerName' in d for d in site['dependsOn'])
    assert container['properties']['publicAccess'] == 'None'
    assert template['parameters']['deploymentSuffix']['defaultValue'] == ''


def test_template_installs_the_prebuilt_package_from_its_release():
    # OneDeploy follows the release download redirect (verified live on Flex);
    # the zip already carries its dependencies, so nothing is built in Azure.
    template, nested = _nested_resources()
    url = template['variables']['functionZipUrl']
    assert url.startswith('https://github.com/site24x7/azure-log-collector/releases/download/v')
    assert url.endswith('/released-package.zip')
    deploy = [r for r in nested if r['type'] == 'Microsoft.Web/sites/extensions']
    assert len(deploy) == 1 and deploy[0]['name'].endswith("'/onedeploy')]")
    assert deploy[0]['properties'] == {'packageUri': "[parameters('functionZipUrl')]", 'remoteBuild': False}


def test_unsupported_region_fails_before_anything_is_created():
    # Flex Consumption is not offered everywhere; a region check runs first, so
    # preflight rejects such a region instead of leaving a locked, half-built group.
    template, _ = _nested_resources()
    check = next(r for r in template['resources'] if r['name'].startswith("[concat('s247-diag-logs-region-check-'"))
    allowed = check['properties']['template']['parameters']['location']['allowedValues']
    assert {'centralindia', 'southindia', 'eastus', 'westeurope'} <= set(allowed)
    assert check['properties']['parameters']['location']['value'] == '[deployment().location]'
    rg = next(r for r in template['resources'] if r['type'] == 'Microsoft.Resources/resourceGroups')
    assert rg['dependsOn'] == [check['name']]


def test_resource_group_lock_is_created_last():
    # A failed deployment must not leave a locked, half-built resource group.
    template, nested = _nested_resources()
    assert not any(r['type'] == 'Microsoft.Authorization/locks' for r in nested)
    last = template['resources'][-1]
    assert last['properties']['template']['resources'][0]['type'] == 'Microsoft.Authorization/locks'
    roles = next(r['name'] for r in template['resources'] if 'sub-roles' in r['name'])
    assert set(last['dependsOn']) == {'s247-diag-logs-resources', roles}


def test_dashboard_output_uses_the_apps_real_host_name():
    template, _ = _nested_resources()
    assert 'defaultHostName' in template['outputs']['dashboardUrl']['value']
    assert 'azurewebsites.net' not in json.dumps(template)


def test_release_build_lock_and_template_use_the_same_python():
    _, nested = _nested_resources()
    site = next(r for r in nested if r['type'] == 'Microsoft.Web/sites')
    runtime = site['properties']['functionAppConfig']['runtime']['version']
    release = (ROOT / '.github/workflows/release-function-app.yml').read_text()
    validate = (ROOT / '.github/workflows/validate-function-app.yml').read_text()
    lock = (ROOT / 'function-app/requirements.lock').read_text()
    assert f'--python-version {runtime} ' in release
    assert release.count(f"python-version: '{runtime}'") == 1
    assert f"python-version: '{runtime}'" in validate
    assert f'CPython {runtime})' in lock and f'--python-version {runtime} ' in lock


@pytest.mark.parametrize('failure', ['global_collision', 'outage', 'ownership_denied'])
def test_regional_preflight_failure_never_creates_account(failure):
    from unittest.mock import MagicMock, patch
    from azure.core.exceptions import ResourceNotFoundError
    from shared.region_manager import RegionManager
    with patch('shared.region_manager.DefaultAzureCredential'), patch('shared.region_manager.StorageManagementClient') as storage:
        operations = storage.return_value.storage_accounts
        if failure == 'outage':
            operations.check_name_availability.side_effect = RuntimeError('Storage API unavailable')
        else:
            operations.check_name_availability.return_value.name_available = False
            operations.get_properties.side_effect = (ResourceNotFoundError('Not found')
                if failure == 'global_collision' else RuntimeError('Read permission denied'))
        result = RegionManager('demo-sub').provision_storage_account('rg', 'eastus', 'qt4xn2')
        assert not result['storage_account_id']
        operations.begin_create.assert_not_called()


def _rg_name(resource_group_name, suffix):
    """Evaluate the template's rgName expression for the given parameters."""
    variables = json.loads((ROOT / 'setup/azuredeploy.json').read_text())['variables']
    expr = variables['rgName']
    assert expr == ("[if(not(empty(trim(parameters('resourceGroupName')))), trim(parameters('resourceGroupName')), "
                    "if(empty(variables('cleanSuffix')), 's247-diag-logs-rg', concat('s247-diag-logs-', variables('cleanSuffix'), '-rg')))]")
    clean = suffix.strip().lower().replace('-', '').replace('_', '').replace('.', '').replace(' ', '')
    if resource_group_name.strip():
        return resource_group_name.strip()
    return 's247-diag-logs-rg' if not clean else f's247-diag-logs-{clean}-rg'


@pytest.mark.parametrize('resource_group_name, suffix, expected', [
    ('', '-Oct14', 's247-diag-logs-oct14-rg'),        # each suffix gets its own group
    ('', '', 's247-diag-logs-rg'),                   # legacy installs keep their group
    ('s247-diag-logs-rg', 'oct14', 's247-diag-logs-rg'),  # redeploy an existing collector in place
])
def test_resource_group_is_per_suffix_with_an_override(resource_group_name, suffix, expected):
    assert _rg_name(resource_group_name, suffix) == expected
    template = json.loads((ROOT / 'setup/azuredeploy.json').read_text())
    assert template['parameters']['resourceGroupName']['defaultValue'] == ''


def test_deployment_form_defaults_and_texts():
    template, nested = _nested_resources()
    params = template['parameters']
    # The portal escapes angle brackets in descriptions, so they would show as &lt; &gt;.
    assert not any('<' in p['metadata']['description'] or '>' in p['metadata']['description'] for p in params.values())
    assert params['confirmAfterDiscovery']['defaultValue'] == 'true'
    assert params['generalLogTypeEnabled']['defaultValue'] == 'false'
    for optional in ('resourceGroupName', 'deploymentSuffix'):
        assert params[optional]['metadata']['description'].startswith('Optional.')
    site = next(r for r in nested if r['type'] == 'Microsoft.Web/sites')
    settings = {s['name']: s['value'] for s in site['properties']['siteConfig']['appSettings']}
    assert settings['REVIEW_BEFORE_COLLECTING'] == "[parameters('confirmAfterDiscovery')]"


def test_portal_form_matches_the_template():
    # setup/createUiDefinition.json gives the portal fields their labels ("... (optional)").
    template, _ = _nested_resources()
    ui = json.loads((ROOT / 'setup/createUiDefinition.json').read_text())['parameters']
    outputs = ui['outputs']
    assert set(outputs) - {'location'} == set(template['parameters'])
    assert outputs['location'] == '[location()]'
    fields = {f['name']: f for f in ui['basics']}
    assert set(fields) == set(template['parameters'])
    assert fields['resourceGroupName']['label'] == 'Resource group name (optional)'
    assert fields['deploymentSuffix']['label'] == 'Deployment suffix (optional)'
    assert fields['site24x7DeviceKey']['type'] == 'Microsoft.Common.PasswordBox'
    # The form offers only the regions the template accepts, and the template's choices.
    check = next(r for r in template['resources'] if r['name'].startswith("[concat('s247-diag-logs-region-check-'"))
    assert ui['config']['basics']['location']['allowedValues'] == check['properties']['template']['parameters']['location']['allowedValues']
    domains = [o['value'] for o in fields['site24x7Domain']['constraints']['allowedValues']]
    assert domains == template['parameters']['site24x7Domain']['allowedValues']
    for name in ('confirmAfterDiscovery', 'generalLogTypeEnabled'):
        values = {o['value'] for o in fields[name]['constraints']['allowedValues']}
        assert values == set(template['parameters'][name]['allowedValues'])


def test_application_insights_uses_the_collectors_own_workspace():
    # Without a workspace Azure creates one in a separate ai_..._managed resource
    # group, with no daily cap, that deleting the collector's group leaves behind.
    _, nested = _nested_resources()
    ws = next(r for r in nested if r['type'] == 'Microsoft.OperationalInsights/workspaces')
    ai = next(r for r in nested if r['type'] == 'Microsoft.Insights/components')
    assert ws['properties']['workspaceCapping']['dailyQuotaGb'] == 1
    assert ws['properties']['retentionInDays'] == 30
    assert ai['properties']['WorkspaceResourceId'] == "[resourceId('Microsoft.OperationalInsights/workspaces', parameters('logWorkspaceName'))]"
    assert ai['dependsOn'] == [ai['properties']['WorkspaceResourceId']]
