"""Guard against stable publication without approval and false health success."""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest



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



def test_release_installs_exact_hashed_dependencies():
    """Every dependency the release ships is pinned to an exact version and SHA-256."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    lock = (root / "requirements.lock").read_text()
    entries = re.findall(r"^([a-z0-9-]+)==(\S+) \\\n((?:\s+--hash=sha256:[0-9a-f]{64}(?: \\)?\n)+)", lock, re.M)
    pinned = {name for name, _, _ in entries}
    wanted = {re.split(r"[<>=!~ ]", line, maxsplit=1)[0].strip().lower().replace("_", "-")
              for line in (root / "requirements.txt").read_text().splitlines()
              if line.strip() and not line.startswith("#")}
    assert wanted <= pinned, wanted - pinned
    workflow = (root.parent / ".github/workflows/release-function-app.yml").read_text()
    assert "--require-hashes" in workflow and "-r requirements.lock" in workflow


def test_release_workflow_never_replaces_a_published_release():
    from pathlib import Path
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/release-function-app.yml").read_text()
    assert "force_release" not in workflow                        # no option to replace
    assert "git push --delete" not in workflow                    # tags are never deleted
    assert "state=published" in workflow and "state=draft" in workflow
    # Every build/publish step is skipped for a published version
    assert workflow.count("if: steps.check_release.outputs.state != 'published'") == 6
    # Only a draft (never served, untagged) may be deleted before it is rebuilt
    delete_at = workflow.index("gh release delete")
    assert 'outputs.state }}" = "draft"' in workflow[delete_at - 200:delete_at]
