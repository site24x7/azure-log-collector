"""Execute preflight and the real settings block with Azure replaced by a stub."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize(('setting', 'key', 'valid'), [
    ('SITE24X7_API_KEY', 'test-device-key', True),
    ('SITE24X7_API_TOKEN', 'test-device-key', True),
    ('SITE24X7_API_KEY', '', False),
    ('SITE24X7_API_KEY', 'your-device-key-here', False),
    ('SITE24X7_API_TOKEN', 'your-token-here', False),
])
def test_setup_validates_keys_and_passes_runtime_api_key(tmp_path, setting, key, valid):
    script = (Path(__file__).parents[2] / 'setup/setup.sh').read_text()
    definitions = script.rsplit('main "$@"', 1)[0]
    # Run the actual settings block, without provisioning any infrastructure.
    settings = script.split('    # ── Function App Settings', 1)[1]
    settings = settings[settings.index('    log_info'):settings.index('\n}')]
    setup = tmp_path / 'setup.sh'
    setup.write_text(definitions)
    (tmp_path / 'config.env').write_text(
        f'SUBSCRIPTION_IDS="test-subscription"\n{setting}="{key}"\n')
    capture = tmp_path / 'arguments.txt'
    runner = tmp_path / 'run.sh'
    runner.write_text('''#!/usr/bin/env bash
set -euo pipefail
az() {
    if [[ "$*" == *"appsettings set"* ]]; then
        printf '%s\\n' "$@" > "$CAPTURE"
    elif [[ "$*" == *"check-name"* ]]; then
        echo true
    fi
}
jq() { :; }
zip() { :; }
source "$SETUP"
LOG_FILE="$SETUP_LOG"
preflight_checks
diag_suffix=test
''' + settings)
    env = {k: v for k, v in os.environ.items() if not k.startswith('SITE24X7_')}
    env.update(SETUP=str(setup), CAPTURE=str(capture), SETUP_LOG=str(tmp_path / "setup.log"))
    result = subprocess.run(['bash', str(runner)], env=env, capture_output=True, text=True)
    if not valid:
        assert result.returncode != 0
        assert 'Set SITE24X7_API_KEY' in result.stdout
        assert not capture.exists()
        return
    assert result.returncode == 0, result.stderr
    args = capture.read_text().splitlines()
    assert 'SITE24X7_API_KEY=test-device-key' in args
    assert not any(arg.startswith('SITE24X7_API_TOKEN=') for arg in args)
    assert 'test-device-key' not in result.stdout
    assert 'test-device-key' not in (tmp_path / 'setup.log').read_text()


@pytest.mark.parametrize(('suffix', 'outcome'), [
    ('c9a6f201a4d9', 'available'),
    ('qt4xn2', 'available'),
    ('bad-name', 'invalid'),
    ('c9a6f201a4d9', 'seed_taken'),
    ('c9a6f201a4d9', 'check_error'),
])
def test_setup_suffix_and_name_preflight(tmp_path, suffix, outcome):
    script = (Path(__file__).parents[2] / 'setup/setup.sh').read_text()
    setup = tmp_path / 'setup.sh'
    setup.write_text(script.rsplit('main "$@"', 1)[0])
    (tmp_path / 'config.env').write_text(
        f'SUBSCRIPTION_IDS="test-subscription"\nSITE24X7_API_KEY="test-key"\nDEPLOYMENT_SUFFIX="{suffix}"\n')
    runner = tmp_path / 'run.sh'
    runner.write_text('''#!/usr/bin/env bash
set -euo pipefail
az() {
    if [[ "$*" == *"check-name"* ]]; then
        echo "$*" >> "$CHECKS"
        [[ "$OUTCOME" != check_error ]] || return 1
        if [[ "$OUTCOME" == seed_taken && "$*" == *s247dr* ]]; then echo false; else echo true; fi
    elif [[ "$*" == *"storage account show"* ]]; then
        return 1
    fi
}
jq() { :; }
zip() { :; }
source "$SETUP"
LOG_FILE="$SETUP_LOG"
preflight_checks
printf '%s\\n' "$DIAG_SUFFIX" "$SEED_STORAGE"
''')
    env = os.environ.copy()
    env.update(SETUP=str(setup), SETUP_LOG=str(tmp_path / 'setup.log'),
               CHECKS=str(tmp_path / 'checks'), OUTCOME=outcome)
    result = subprocess.run(['bash', str(runner)], env=env, capture_output=True, text=True)
    if outcome != 'available':
        assert result.returncode != 0
        if outcome == 'seed_taken':
            assert 'DEPLOYMENT_SUFFIX' in result.stdout
        if outcome == 'check_error':
            assert 'Unable to check' in result.stdout
        return
    assert result.returncode == 0, result.stderr
    names = (tmp_path / 'checks').read_text()
    assert f's247diag{suffix}' in names
    assert (f's247dr{suffix}' if len(suffix) > 6 else f's247diageastus{suffix}') in names
    assert (f's247dt{suffix}' if len(suffix) > 6 else f's247diagtenant{suffix}') in names
    assert f'\n{suffix}\n' in result.stdout
