"""Guard against stable publication without approval and false health success."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.updater import _post_deploy_health_check


def release_gate():
    path = Path(__file__).parents[2] / 'setup/check-release-approval.py'
    spec = importlib.util.spec_from_file_location('release_approval', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_approval


@pytest.mark.parametrize(('version', 'event', 'confirmed', 'evidence', 'allowed'), [
    ('1.0.1', 'push', 'true', 'https://example.com/report', False),
    ('1.0.1', 'workflow_dispatch', 'false', 'https://example.com/report', False),
    ('1.0.1', 'workflow_dispatch', 'true', '', False),
    ('1.0.1', 'workflow_dispatch', 'true', 'http://example.com/report', False),
    ('1.0.1', 'workflow_dispatch', 'true', 'https://user:secret@example.com/report', False),
    ('1.0.1', 'workflow_dispatch', 'true', 'https://example.com/report', True),
    ('1.0.1-rc1', 'push', '', '', True),
    ('invalid', 'workflow_dispatch', 'true', 'https://example.com/report', False),
])
def test_release_approval(version, event, confirmed, evidence, allowed):
    if allowed:
        release_gate()(version, event, confirmed, evidence)
    else:
        with pytest.raises(ValueError):
            release_gate()(version, event, confirmed, evidence)


@pytest.mark.parametrize('response', ['healthy', 'bad_dependencies', 'missing_fields', 'html', 'unauthorized', 'network'])
def test_health_requires_authenticated_alive_response_and_healthy_dependencies(monkeypatch, response):
    monkeypatch.setenv('SUBSCRIPTION_IDS', 'demo-sub')
    monkeypatch.setenv('RESOURCE_GROUP_NAME', 'demo-rg')
    with patch('azure.identity.DefaultAzureCredential'), patch('azure.mgmt.web.WebSiteManagementClient') as web, \
         patch('shared.updater.time.sleep'), patch('shared.updater.requests.get') as get:
        web.return_value.web_apps.list_host_keys.return_value.function_keys = {'default': 'test-private-key'}
        resp = get.return_value
        resp.ok = response != 'unauthorized'
        resp.status_code = 401 if response == 'unauthorized' else 200
        resp.json.return_value = ({'status': 'alive', 'deps_ok': response != 'bad_dependencies'}
                                  if response != 'missing_fields' else {})
        if response == 'html':
            resp.json.side_effect = ValueError('Not JSON')
        if response == 'network':
            get.side_effect = RuntimeError('unreachable')
        result = _post_deploy_health_check('demo-app', checks=1)
        assert result['healthy'] is (response == 'healthy')
        assert 'test-private-key' not in str(result)
        get.assert_called_once_with('https://demo-app.azurewebsites.net/api/health',
                                    headers={'x-functions-key': 'test-private-key'}, timeout=15)
        web.return_value.web_apps.list_host_keys.assert_called_once_with('demo-rg', 'demo-app')


def test_health_key_failure_returns_unhealthy_without_leaking_secrets():
    with patch('azure.identity.DefaultAzureCredential'), patch('azure.mgmt.web.WebSiteManagementClient') as web, \
         patch('shared.updater.time.sleep'), patch('shared.updater.requests.get') as get:
        web.return_value.web_apps.list_host_keys.side_effect = RuntimeError('secret should not leak')
        result = _post_deploy_health_check('demo-app', checks=1)
        assert result['healthy'] is False
        assert 'secret should not leak' not in str(result)
        get.assert_not_called()
