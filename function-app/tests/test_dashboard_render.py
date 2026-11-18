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


def test_failing_uploads_are_not_shown_as_healthy():
    # Site24x7 shows Needs Attention when uploads fail; the dashboard must not say Healthy / Connected then.
    js = _rendered_js()
    assert "op.upload_status === 'failed'" in js and "op.upload_refused === true" in js
    assert "S247 Uploads Failing" in js
    assert js.index("uploadFailing") < js.index("if (_s247Problem) { showDegraded(_s247Problem); }")


def _render():
    resp = Dashboard.main(MagicMock(params={}, headers={}, method="GET"))
    return resp.headers["Content-Security-Policy"], resp.get_body().decode()


def test_csp_uses_a_per_request_script_nonce():
    csp, html = _render()
    script_src = next(d.strip() for d in csp.split(";") if d.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_src
    m = re.fullmatch(r"script-src 'nonce-([A-Za-z0-9_-]{16,})'", script_src)
    assert m, script_src
    nonce = m.group(1)
    assert re.findall(r"<script\b[^>]*>", html) == [f'<script nonce="{nonce}">']
    assert "__CSP_NONCE__" not in html
    assert "style-src 'unsafe-inline'" in csp

    csp2, _ = _render()
    assert csp2 != csp


def test_dashboard_html_has_no_inline_event_handlers():
    _, html = _render()
    assert not re.search(r"\son[a-z]+\s*=", html.split("<script", 1)[0])
    assert "javascript:" not in html
