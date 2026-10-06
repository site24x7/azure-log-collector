"""Exercise the Entra HTTP toggle with storage and Site24x7 failure paths."""
import json
from unittest.mock import MagicMock

import azure.functions as func
import pytest

from UpdateEntraLogTypes import main
from shared import config_store, site24x7_client


@pytest.fixture
def deps(monkeypatch):
    mocks = {}
    for name, value in {
        'get_supported_log_types': {'auditlogs': {'logtype': 'auditlogs'}},
        'get_entra_logtype_states': {},
        'get_logtype_config': None,
        'save_logtype_config': True,
        'delete_logtype_config': True,
    }.items():
        mocks[name] = MagicMock(return_value=value)
        monkeypatch.setattr(config_store, name, mocks[name])
    mocks['set_entra_logtype_state'] = MagicMock(side_effect=lambda cat, state: {cat: state})
    monkeypatch.setattr(config_store, 'set_entra_logtype_state', mocks['set_entra_logtype_state'])
    mocks['client'] = MagicMock()
    mocks['client'].create_log_types.return_value = [
        {'category': 'S247_auditlogs', 'sourceConfig': {'logType': 'auditlogs'}}]
    monkeypatch.setattr(site24x7_client, 'Site24x7Client', lambda: mocks['client'])
    return mocks


def toggle(action):
    req = func.HttpRequest(method='POST', url='https://example.invalid/api/entra-logtypes',
                           body=json.dumps({'action': action, 'category': 'auditlogs'}).encode())
    return main(req)


def test_enable_commits_config_and_state_before_reporting_success(deps):
    response = toggle('enable')
    payload = json.loads(response.get_body())
    assert response.status_code == 200
    assert payload['enabled'] is True
    assert payload['states']['auditlogs']['status'] == 'created'
    deps['save_logtype_config'].assert_called_once()
    deps['get_supported_log_types'].assert_called_once_with(force_refresh=True)


def test_failed_config_save_never_reports_created_or_enabled(deps):
    deps['save_logtype_config'].return_value = False
    assert toggle('enable').status_code == 500
    states = [c.args[1] for c in deps['set_entra_logtype_state'].call_args_list]
    assert states and all(s['enabled'] is False and s['status'] == 'failed' for s in states)


def test_creation_exception_does_not_enable_category(deps):
    deps['client'].create_log_types.side_effect = RuntimeError('network unavailable')
    assert toggle('enable').status_code == 500
    assert deps['set_entra_logtype_state'].call_args.args[1]['enabled'] is False


def test_failed_delete_preserves_existing_state(deps):
    deps['get_entra_logtype_states'].return_value = {'auditlogs': {'enabled': True, 'status': 'created'}}
    deps['delete_logtype_config'].return_value = False
    assert toggle('disable').status_code == 500
    deps['set_entra_logtype_state'].assert_not_called()


def test_failed_enable_state_commit_removes_new_config(deps):
    deps['set_entra_logtype_state'].return_value = None
    deps['set_entra_logtype_state'].side_effect = None
    assert toggle('enable').status_code == 500
    deps['delete_logtype_config'].assert_called_once_with('auditlogs')


def test_failed_disable_state_commit_restores_prior_config(deps):
    old_config = {'logType': 'auditlogs', 'path': 'properties.message'}
    deps['get_logtype_config'].return_value = old_config
    deps['get_entra_logtype_states'].return_value = {'auditlogs': {'enabled': True, 'status': 'created'}}
    deps['set_entra_logtype_state'].side_effect = RuntimeError('storage unavailable')
    assert toggle('disable').status_code == 500
    deps['save_logtype_config'].assert_called_once_with('auditlogs', old_config)


def test_failed_retry_preserves_existing_enabled_category(deps):
    deps['get_entra_logtype_states'].return_value = {'auditlogs': {'enabled': True, 'status': 'created'}}
    deps['client'].create_log_types.side_effect = RuntimeError('server unavailable')
    assert toggle('enable').status_code == 500
    deps['set_entra_logtype_state'].assert_not_called()


def test_disable_success(deps):
    response = toggle('disable')
    assert response.status_code == 200
    assert json.loads(response.get_body())['enabled'] is False
    deps['client'].create_log_types.assert_not_called()


def test_failed_failure_state_write_does_not_escape_endpoint(deps):
    deps['client'].create_log_types.side_effect = RuntimeError('server unavailable')
    deps['set_entra_logtype_state'].side_effect = RuntimeError('storage unavailable')
    assert toggle('enable').status_code == 500


def test_missing_server_config_stays_disabled(deps):
    deps['client'].create_log_types.return_value = []
    response = toggle('enable')
    payload = json.loads(response.get_body())
    assert payload['enabled'] is False
    assert payload['status'] == 'failed'
    deps['save_logtype_config'].assert_not_called()


def test_failed_enable_state_commit_restores_existing_config(deps):
    old_config = {'logType': 'auditlogs', 'path': 'old-schema'}
    deps['get_logtype_config'].return_value = old_config
    deps['get_entra_logtype_states'].return_value = {'auditlogs': {'enabled': True, 'status': 'created'}}
    deps['set_entra_logtype_state'].side_effect = None
    deps['set_entra_logtype_state'].return_value = None
    assert toggle('enable').status_code == 500
    assert deps['save_logtype_config'].call_args_list[-1].args == ('auditlogs', old_config)
    deps['delete_logtype_config'].assert_not_called()
