"""The dashboard page is one Python string of HTML + JS: render it and check the JS parses."""

import re
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

import Dashboard


def _rendered_js():
    req = MagicMock(params={}, headers={}, method="GET")
    resp = Dashboard.main(req)
    assert resp.status_code == 200
    return "\n;\n".join(re.findall(r"<script[^>]*>(.*?)</script>", resp.get_body().decode(), re.S))


def test_dashboard_explains_a_dashboard_only_key():
    js = _rendered_js()
    assert "r.status === 401" in js and "showAuthHelp()" in js
    assert "Host keys > default" in js


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_rendered_dashboard_javascript_parses(tmp_path):
    script = tmp_path / "dashboard.js"
    script.write_text(_rendered_js())
    result = subprocess.run(["node", "--check", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_site24x7_rejection_marks_status_degraded_whatever_loads_first():
    js = _rendered_js()
    assert "_s247Problem = s.s247_reachable === false" in js
    assert "if (!failed.length && _s247Problem)" in js
