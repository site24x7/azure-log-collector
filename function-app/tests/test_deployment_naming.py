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
    assert _storage_account_name(region, 'qt4xn2') == f's247diag{region}qt4xn2'[:24]
    assert _tenant_storage_name('qt4xn2') == 's247diagtenantqt4xn2'


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


def test_template_runs_from_direct_package_url():
    # Linux Consumption refuses to be created with a redirecting package URL
    # (GitHub release assets 302), so the template uses the raw.githubusercontent
    # copy on the packages branch, which is served directly.
    template = json.loads((ROOT / 'setup/azuredeploy.json').read_text())
    assert template['parameters']['deploymentSuffix']['defaultValue'] == ''
    assert template['variables']['functionZipUrl'].startswith(
        'https://raw.githubusercontent.com/site24x7/azure-log-collector/packages/v')
    nested = template['resources'][1]['properties']['template']['resources']
    site = next(r for r in nested if r['type'] == 'Microsoft.Web/sites')
    settings = {s['name']: s['value'] for s in site['properties']['siteConfig']['appSettings']}
    assert settings['WEBSITE_RUN_FROM_PACKAGE'] == "[parameters('functionZipUrl')]"
    assert 'SCM_DO_BUILD_DURING_DEPLOYMENT' not in settings
    assert 'ENABLE_ORYX_BUILD' not in settings
    assert settings['DIAG_SEED_REGION'] == "[parameters('location')]"
    assert not any(r['type'] == 'Microsoft.Web/sites/extensions' for r in nested)


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
