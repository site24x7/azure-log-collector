"""Fixes for the 1.1.1 re-review: shell owner tags, ON during a removal, the
shell destination check and the shell install's Flex plan tags."""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest
import ScanWorker
from shared import collection, teardown
from tests.test_delete_collector import _res

REPO = Path(__file__).resolve().parents[2]
SUB = '00000000-0000-0000-0000-000000000001'
OTHER_SUB = '00000000-0000-0000-0000-000000000002'
APP = 's247-diag-func-qt4xn2'
RG = 's247-diag-logs-rg'
MANAGED = {'managed-by': 's247-diag-logs'}


@pytest.fixture(autouse=True)
def safe_env(monkeypatch):
    for key in ('AzureWebJobsStorage', 'IDENTITY_ENDPOINT', 'MSI_ENDPOINT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('SUBSCRIPTION_IDS', SUB)
    monkeypatch.setenv('RESOURCE_GROUP_NAME', RG)
    monkeypatch.setenv('WEBSITE_SITE_NAME', APP)
    monkeypatch.setenv('DIAG_STORAGE_SUFFIX', 'qt4xn2')


def _shell(tmp_path, resources, locks=None, cleanup=False, destination=''):
    """Execute the real teardown main() with EVERY az operation intercepted."""
    definitions = tmp_path / 'definitions.sh'
    definitions.write_text((REPO / 'setup/teardown.sh').read_text().rsplit('main "$@"', 1)[0])
    (tmp_path / 'config.env').write_text(
        f'SUBSCRIPTION_IDS="{SUB}"\nRESOURCE_GROUP_NAME="{RG}"\nDEPLOYMENT_SUFFIX="qt4xn2"\n')
    resource_path = tmp_path / 'resources.json'
    resource_path.write_text(json.dumps(resources))
    lock_path = tmp_path / 'locks.json'
    lock_path.write_text(json.dumps(locks or []))
    calls = tmp_path / 'az-calls.txt'
    runner = tmp_path / 'runner.sh'
    runner.write_text(r'''
#!/usr/bin/env bash
source "$AUDIT_DIR/definitions.sh"
SCRIPT_DIR="$AUDIT_DIR"
az() {
    printf '%s\n' "$*" >> "$AUDIT_DIR/az-calls.txt"
    case "$1 $2" in
        'account show') printf '{}\n' ;;
        'group show')
            if [[ "$*" == *--query* ]]; then printf 's247-diag-logs\n'; else printf '{}\n'; fi ;;
        'resource list') cat "$AUDIT_DIR/resources.json" ;;
        'lock list') cat "$AUDIT_DIR/locks.json" ;;
        'functionapp list') printf 'own-principal\nother-principal\n' ;;
        'role assignment')
            if [[ "$3" == list ]]; then printf '/subscriptions/test/providers/Microsoft.Authorization/roleAssignments/fake\n'; fi ;;
        'monitor diagnostic-settings')
            if [[ "$3" == show ]]; then printf '%s\n' "$AUDIT_DESTINATION"; fi ;;
        'lock delete'|'group delete') return 0 ;;
        *) printf 'UNEXPECTED AZ OPERATION\n' >&2; return 99 ;;
    esac
}
if [[ "$AUDIT_CLEANUP" == 1 ]]; then CLEAN_DIAG_SETTINGS=true; fi
main
''')
    result = subprocess.run(['bash', str(runner)], capture_output=True, text=True,
                            env={**os.environ, 'AUDIT_DIR': str(tmp_path),
                                 'AUDIT_CLEANUP': '1' if cleanup else '0',
                                 'AUDIT_DESTINATION': destination})
    assert 'UNEXPECTED AZ OPERATION' not in result.stderr, result.stderr
    return result, calls.read_text() if calls.exists() else ''


def _r(name, kind, tags=None):
    return {'name': name, 'type': kind, 'tags': tags or {}, 'location': 'eastus',
            'id': f'/subscriptions/{SUB}/resourceGroups/{RG}/providers/{kind}/{name}'}


def _own_shell_resources():
    return [_r(APP, 'Microsoft.Web/sites', MANAGED),
            _r(APP + '-plan', 'Microsoft.Web/serverFarms', MANAGED)]



def test_shell_refuses_storage_owned_by_another_collector(tmp_path):
    other = _r('s247diageastusother1', 'Microsoft.Storage/storageAccounts',
               dict(MANAGED, purpose='diag-logs-regional', **{'s247-collector': 's247-diag-func-other1'}))
    result, calls = _shell(tmp_path, _own_shell_resources() + [other])
    assert result.returncode == 1
    assert 's247diageastusother1' in result.stdout
    assert 'group delete' not in calls and 'lock delete' not in calls
    assert teardown.foreign_resources([_res(other['name'], other['type'], other['tags'])])


def test_shell_accepts_resources_owned_by_its_own_app(tmp_path):
    own = _r('s247diageastusqt4xn2', 'Microsoft.Storage/storageAccounts',
             dict(MANAGED, purpose='diag-logs-regional', **{'s247-collector': APP.upper()}))
    result, calls = _shell(tmp_path, _own_shell_resources() + [own])
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'group delete' in calls


@contextmanager
def _collection_world():
    state = {'enabled': True}
    processing = []
    clear_when_enabled = []
    def update(patch):
        state.update(patch)
        return dict(state)
    def clear():
        clear_when_enabled.append(state['enabled'])
        return {'accounts': 1, 'containers_cleared': 1, 'storage_errors': 0}
    with ExitStack() as stack:
        stack.enter_context(patch('shared.config_store.update_collection_state', side_effect=update))
        stack.enter_context(patch('shared.config_store.get_collection_state', side_effect=lambda **kw: dict(state)))
        stack.enter_context(patch('shared.config_store.save_processing_enabled', side_effect=lambda value: processing.append(value) or True))
        for target, value in [
            ('shared.config_store.try_acquire_scan_lock', True),
            ('shared.config_store.get_configured_resources', {}),
            ('shared.config_store.unmark_resource_configured', True),
            ('shared.subscriptions.monitored_subscriptions', ([SUB], {}, None)),
        ]:
            stack.enter_context(patch(target, return_value=value))
        manager = stack.enter_context(patch('shared.azure_manager.AzureManager'))
        stack.enter_context(patch('shared.collection._clear_all', side_effect=clear))
        for target in ('ScanWorker._clear_in_progress', 'shared.registration.register_with_site24x7',
                       'shared.debug_logger.log_event', 'shared.debug_logger.log_audit'):
            stack.enter_context(patch(target))
        manager.return_value.remove_all_diagnostic_settings.return_value = {
            'removed': 1, 'errors': 0, 'unresolved': 0, 'resolved_ids': []}
        yield state, processing, clear_when_enabled, manager.return_value


def _message(body):
    msg = MagicMock()
    msg.dequeue_count = 1
    msg.get_body.return_value = json.dumps(body).encode()
    return msg



def test_on_during_removal_keeps_the_stored_logs():
    with _collection_world() as (state, processing, clears, manager):
        old = collection.request_change(False)
        def remove_then_user_turns_on(*args):
            collection.request_change(True)
            return {'removed': 1, 'errors': 0, 'unresolved': 0, 'resolved_ids': []}
        manager.remove_all_diagnostic_settings.side_effect = remove_then_user_turns_on
        ScanWorker.main(_message(old))
        assert state['enabled'] is True and processing[-1] is True
        assert clears == []
        assert state['stop'] is None


def test_unreadable_state_before_clearing_keeps_the_stored_logs():
    with _collection_world() as (state, processing, clears, manager):
        old = collection.request_change(False)
        reads = []
        def read(**kw):
            reads.append(1)
            if len(reads) > 1:
                raise RuntimeError('table down')
            return dict(state)
        with patch('shared.config_store.get_collection_state', side_effect=read):
            ScanWorker.main(_message(old))
        assert clears == []
        assert state['stop']['state'] == 'partial'


def test_off_still_clears_storage_when_it_stays_off():
    with _collection_world() as (state, processing, clears, manager):
        ScanWorker.main(_message(collection.request_change(False)))
        assert clears == [False]
        assert state['stop']['state'] == 'done'


def test_shell_cleanup_skips_destination_in_another_subscription(tmp_path):
    destination = f'/subscriptions/{OTHER_SUB}/resourceGroups/{RG}/providers/Microsoft.Storage/storageAccounts/customerarchive'
    result, calls = _shell(tmp_path, _own_shell_resources(), cleanup=True, destination=destination)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'monitor diagnostic-settings delete' not in calls


def test_shell_cleanup_removes_setting_sending_to_its_own_group(tmp_path):
    destination = f'/subscriptions/{SUB}/resourceGroups/{RG.upper()}/providers/Microsoft.Storage/storageAccounts/s247diageastusqt4xn2'
    result, calls = _shell(tmp_path, _own_shell_resources(), cleanup=True, destination=destination)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'monitor diagnostic-settings delete' in calls


def test_shell_install_tags_its_flex_plan_with_the_owner():
    setup = (REPO / 'setup/setup.sh').read_text()
    assert '--query serverFarmId' in setup
    assert 's247-collector="$(echo "$FUNC_APP"' in setup
    plan = _res('ASP-s247diaglogsrg-1a2b', 'Microsoft.Web/serverFarms',
                dict(MANAGED, **{'s247-collector': APP}))
    assert teardown.foreign_resources([plan]) == []
