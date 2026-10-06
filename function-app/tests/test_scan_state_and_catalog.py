"""Scan regressions: preserve lock/results and discover newly supported types."""
from unittest.mock import MagicMock, patch

from DiagSettingsManager import _refresh_supported_types, _save_early_scan_state
from shared import config_store


def test_discovery_progress_preserves_existing_state_and_lock():
    old = {
        'scan_started_at': '2026-10-06T12:00:00+00:00',
        'last_scan_time': '2026-10-05T12:00:00+00:00',
        's247_reachable': True, 's247_errors': [], 'newly_configured': 12,
        'entra_target_storage_account_id': '/subscriptions/test/storage/tenant',
    }
    saved = dict(old)
    def rmw(blob, mutate, default=None):
        saved.update(mutate(saved))
        return saved
    with patch.object(config_store, '_rmw_blob', side_effect=rmw):
        _save_early_scan_state([{}, {}, {}], [{}, {}], 1)
    assert all(saved[k] == v for k, v in old.items())
    assert saved['total_resources'] == 3
    assert saved['active_resources'] == 2
    assert saved['ignored_resources'] == 1
    assert saved['in_progress'] is True
    assert saved['current_phase'] == 3


def test_scan_refreshes_populated_catalog_and_persists_new_categories():
    client = MagicMock()
    entries = [{'logtype': 'auditlogs'}, {'logtype': 'signinlogs'}]
    client.get_supported_log_types.return_value = {'supported_types': entries}
    with patch.object(config_store, 'get_supported_log_types', return_value={'auditlogs': entries[0]}), \
         patch.object(config_store, 'save_supported_log_types', return_value=True) as save:
        refreshed = _refresh_supported_types(client)
    assert 'signinlogs' in refreshed
    client.get_supported_log_types.assert_called_once()
    save.assert_called_once_with(refreshed)


def test_catalog_parent_alias_wins_over_self_referencing_category():
    client = MagicMock()
    parent = {'logtype': 'runbook', 'log_categories': ['Job Logs']}
    client.get_supported_log_types.return_value = {
        'supported_types': [{'logtype': 'joblogs'}, parent]}
    with patch.object(config_store, 'get_supported_log_types', return_value={}), \
         patch.object(config_store, 'save_supported_log_types', return_value=True):
        assert _refresh_supported_types(client)['joblogs'] == parent


def test_catalog_outage_or_bad_response_keeps_existing_types():
    cached = {'auditlogs': {'logtype': 'auditlogs'}}
    client = MagicMock()
    for response in (None, {}, {'supported_types': []}, {'supported_types': ['invalid']}):
        client.get_supported_log_types.return_value = response
        with patch.object(config_store, 'get_supported_log_types', return_value=cached), \
             patch.object(config_store, 'save_supported_log_types') as save:
            assert _refresh_supported_types(client) == cached
            save.assert_not_called()
    client.get_supported_log_types.side_effect = RuntimeError('server unavailable')
    with patch.object(config_store, 'get_supported_log_types', return_value=cached):
        assert _refresh_supported_types(client) == cached


def test_catalog_persistence_failure_still_uses_fresh_server_types_for_scan():
    client = MagicMock()
    client.get_supported_log_types.return_value = {'supported_types': [{'logtype': 'signinlogs'}]}
    with patch.object(config_store, 'get_supported_log_types', return_value={'auditlogs': {}}), \
         patch.object(config_store, 'save_supported_log_types', return_value=False):
        assert 'signinlogs' in _refresh_supported_types(client)
