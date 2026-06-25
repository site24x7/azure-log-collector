"""Tests for shared/updater.py — version logic + mocked HTTP/Azure."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from shared.updater import (
    get_local_version,
    parse_version,
    is_update_available,
    fetch_remote_version,
    deploy_update,
    check_and_apply_update,
    _resolve_update_url,
    _parse_github_release,
    _package_url_trusted,
    _sha256_of_file,
)


# ─── parse_version ──────────────────────────────────────────────────────────


class TestParseVersion:
    def test_normal_semver(self):
        assert parse_version("1.2.3") == (1, 2, 3, 1, 0, 0)

    def test_major_only(self):
        assert parse_version("5") == (5, 1, 0, 0)

    def test_two_part(self):
        assert parse_version("2.0") == (2, 0, 1, 0, 0)

    def test_invalid(self):
        assert parse_version("not.a.ver") == (0, 0, 0, 0, 0, 0)

    def test_none(self):
        assert parse_version(None) == (0, 0, 0, 0, 0, 0)

    def test_empty(self):
        assert parse_version("") == (0, 0, 0, 0, 0, 0)

    def test_prerelease_alpha(self):
        assert parse_version("0.1.0-alpha.1") == (0, 1, 0, 0, 1, 1)

    def test_prerelease_beta(self):
        assert parse_version("2.0.0-beta") == (2, 0, 0, 0, 2, 0)

    def test_prerelease_rc(self):
        assert parse_version("1.3.0-rc.2") == (1, 3, 0, 0, 3, 2)

    def test_leading_v(self):
        assert parse_version("v1.2.3") == (1, 2, 3, 1, 0, 0)

    def test_leading_v_with_prerelease(self):
        assert parse_version("v0.1.0-alpha.1") == (0, 1, 0, 0, 1, 1)

    def test_alpha2_lt_alpha3(self):
        assert parse_version("0.1.0-alpha.2") < parse_version("0.1.0-alpha.3")

    def test_prerelease_lt_release(self):
        assert parse_version("0.1.0-alpha.9") < parse_version("0.1.0")

    def test_alpha3_gt_alpha2(self):
        assert parse_version("0.1.0-alpha.3") > parse_version("0.1.0-alpha.2")

    def test_alpha_lt_beta(self):
        # alpha9 must sort BELOW beta2 — the bug that caused
        # "Up to date" when running alpha9 with beta2 on GitHub.
        assert parse_version("1.0.5-alpha9") < parse_version("1.0.5-beta2")
        assert parse_version("1.0.5-alpha.99") < parse_version("1.0.5-beta.1")

    def test_beta_lt_rc(self):
        assert parse_version("1.0.5-beta9") < parse_version("1.0.5-rc1")

    def test_suffix_only_prerelease_compare(self):
        # Suffix-only forms (no dot separator) sort correctly across labels.
        assert parse_version("1.0.5-alpha7") < parse_version("1.0.5-alpha9")
        assert parse_version("1.0.5-beta1") < parse_version("1.0.5-beta2")


# ─── is_update_available ────────────────────────────────────────────────────


class TestIsUpdateAvailable:
    def test_newer_available(self):
        assert is_update_available("1.0.0", "1.1.0") is True

    def test_same_version(self):
        assert is_update_available("1.0.0", "1.0.0") is False

    def test_older_remote(self):
        assert is_update_available("2.0.0", "1.0.0") is False

    def test_patch_bump(self):
        assert is_update_available("1.0.0", "1.0.1") is True

    def test_major_bump(self):
        assert is_update_available("1.9.9", "2.0.0") is True

    def test_prerelease_to_newer(self):
        assert is_update_available("0.1.0-alpha.1", "0.2.0") is True

    def test_same_prerelease(self):
        assert is_update_available("0.1.0-alpha.1", "0.1.0-alpha.2") is True  # alpha.2 > alpha.1


# ─── _resolve_update_url ────────────────────────────────────────────────────


class TestResolveUpdateUrl:
    def test_full_https_url(self):
        url = "https://example.com/version.json"
        assert _resolve_update_url(url) == url

    def test_github_shorthand(self):
        assert _resolve_update_url("owner/repo") == "https://api.github.com/repos/owner/repo/releases/latest"

    def test_github_shorthand_with_dots(self):
        assert _resolve_update_url("my-org/my-repo.v2") == "https://api.github.com/repos/my-org/my-repo.v2/releases/latest"

    def test_whitespace_trimmed(self):
        assert _resolve_update_url("  owner/repo  ") == "https://api.github.com/repos/owner/repo/releases/latest"


# ─── _parse_github_release ──────────────────────────────────────────────────


class TestParseGithubRelease:
    def test_with_zip_asset(self):
        data = {
            "tag_name": "v1.2.0",
            "body": "Bug fixes",
            "assets": [
                {
                    "name": "s247-function-app.zip",
                    "browser_download_url": "https://github.com/o/r/releases/download/v1.2.0/s247-function-app.zip",
                }
            ],
        }
        result = _parse_github_release(data)
        assert result["version"] == "1.2.0"
        assert result["package_url"].endswith(".zip")
        assert result["release_notes"] == "Bug fixes"

    def test_no_zip_asset(self):
        data = {"tag_name": "v1.0.0", "body": "", "assets": []}
        assert _parse_github_release(data) is None

    def test_no_tag(self):
        data = {"body": "", "assets": []}
        assert _parse_github_release(data) is None

    def test_fallback_to_any_zip(self):
        data = {
            "tag_name": "v2.0.0",
            "body": "",
            "assets": [
                {"name": "README.md", "browser_download_url": "https://example.com/README.md"},
                {"name": "deploy.zip", "browser_download_url": "https://example.com/deploy.zip"},
            ],
        }
        result = _parse_github_release(data)
        assert result["version"] == "2.0.0"
        assert result["package_url"] == "https://example.com/deploy.zip"


# ─── get_local_version ──────────────────────────────────────────────────────


class TestGetLocalVersion:
    @patch("shared.updater.VERSION_FILE")
    def test_reads_version_file(self, mock_path):
        mock_path.read_text.return_value = "1.2.3\n"
        assert get_local_version() == "1.2.3"

    @patch("shared.updater.VERSION_FILE")
    def test_fallback_on_error(self, mock_path):
        mock_path.read_text.side_effect = FileNotFoundError()
        assert get_local_version() == "0.0.0"


# ─── fetch_remote_version ───────────────────────────────────────────────────


class TestFetchRemoteVersion:
    @patch("shared.updater.requests.get")
    def test_direct_version_json(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "version": "2.0.0",
            "package_url": "https://example.com/v2.zip",
            "release_notes": "Bug fixes",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_remote_version("https://example.com/version.json")
        assert result["version"] == "2.0.0"
        assert result["package_url"] == "https://example.com/v2.zip"

    @patch("shared.updater.requests.get")
    def test_github_releases_api(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "tag_name": "v3.0.0",
            "body": "Major update",
            "assets": [
                {
                    "name": "s247-function-app.zip",
                    "browser_download_url": "https://github.com/o/r/releases/download/v3.0.0/s247-function-app.zip",
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_remote_version("https://api.github.com/repos/o/r/releases/latest")
        assert result["version"] == "3.0.0"
        assert "s247-function-app.zip" in result["package_url"]

    @patch("shared.updater.requests.get")
    def test_github_shorthand(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "tag_name": "v1.5.0",
            "body": "",
            "assets": [
                {
                    "name": "s247-function-app.zip",
                    "browser_download_url": "https://github.com/o/r/releases/download/v1.5.0/s247-function-app.zip",
                },
            ],
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = fetch_remote_version("owner/repo")
        assert result["version"] == "1.5.0"

    @patch("shared.updater.requests.get")
    def test_missing_fields(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"version": "2.0.0"}  # missing package_url
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        assert fetch_remote_version("https://example.com/version.json") is None

    @patch("shared.updater.requests.get")
    def test_network_error(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")
        assert fetch_remote_version("https://example.com/version.json") is None


# ─── deploy_update ───────────────────────────────────────────────────────────


class TestDeployUpdate:
    def test_no_func_app_name(self, monkeypatch):
        monkeypatch.setenv("SUBSCRIPTION_IDS", "sub1")
        result = deploy_update("https://example.com/pkg.zip")
        assert result["success"] is False
        assert "WEBSITE_SITE_NAME" in result["error"]

    def test_no_subscription(self, monkeypatch):
        monkeypatch.setenv("WEBSITE_SITE_NAME", "myapp")
        result = deploy_update("https://example.com/pkg.zip")
        assert result["success"] is False
        assert "subscription" in result["error"].lower()

    def _ready_env(self, monkeypatch):
        """App identity present so we reach the integrity gates."""
        monkeypatch.setenv("WEBSITE_SITE_NAME", "myapp")
        monkeypatch.setenv("SUBSCRIPTION_IDS", "sub1")

    def test_rejects_non_https_package_url(self, monkeypatch):
        self._ready_env(monkeypatch)
        monkeypatch.setenv("UPDATE_CHECK_URL", "site24x7/azure-log-collector")
        result = deploy_update("http://github.com/site24x7/azure-log-collector/releases/download/v1/s247-function-app.zip")
        assert result["success"] is False
        assert result.get("integrity_failed") is True
        assert "https" in result["error"].lower()

    def test_rejects_untrusted_host(self, monkeypatch):
        self._ready_env(monkeypatch)
        monkeypatch.setenv("UPDATE_CHECK_URL", "site24x7/azure-log-collector")
        result = deploy_update("https://evil.example.com/s247-function-app.zip")
        assert result["success"] is False
        assert result.get("integrity_failed") is True

    def test_rejects_wrong_github_repo(self, monkeypatch):
        self._ready_env(monkeypatch)
        monkeypatch.setenv("UPDATE_CHECK_URL", "site24x7/azure-log-collector")
        # Trusted host, but a different repo than the configured update source.
        result = deploy_update("https://github.com/attacker/evil/releases/download/v1/s247-function-app.zip")
        assert result["success"] is False
        assert result.get("integrity_failed") is True

    def test_requires_sha256_when_absent(self, monkeypatch):
        self._ready_env(monkeypatch)
        monkeypatch.setenv("UPDATE_CHECK_URL", "site24x7/azure-log-collector")
        # REQUIRE_PACKAGE_SHA256 defaults to true; no digest passed → refuse.
        result = deploy_update("https://github.com/site24x7/azure-log-collector/releases/download/v1/s247-function-app.zip")
        assert result["success"] is False
        assert result.get("integrity_failed") is True
        assert "sha256" in result["error"].lower()

    @patch("shared.updater.requests.get")
    def test_rejects_sha256_mismatch(self, mock_get, monkeypatch):
        self._ready_env(monkeypatch)
        monkeypatch.setenv("UPDATE_CHECK_URL", "site24x7/azure-log-collector")
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.iter_content.return_value = [b"the actual package bytes"]
        mock_get.return_value = resp
        result = deploy_update(
            "https://github.com/site24x7/azure-log-collector/releases/download/v1/s247-function-app.zip",
            expected_sha256="0" * 64,  # deliberately wrong
        )
        assert result["success"] is False
        assert result.get("integrity_failed") is True
        assert "mismatch" in result["error"].lower()


class TestPackageUrlTrusted:
    REPO = "site24x7/azure-log-collector"
    GOOD = "https://github.com/site24x7/azure-log-collector/releases/download/v1.0.1/s247-function-app.zip"

    def test_github_shorthand_happy_path(self):
        ok, _ = _package_url_trusted(self.GOOD, self.REPO)
        assert ok is True

    def test_github_cdn_host_allowed(self):
        ok, _ = _package_url_trusted(
            "https://objects.githubusercontent.com/github-production-release-asset/abc/def", self.REPO
        )
        assert ok is True

    def test_http_rejected(self):
        ok, reason = _package_url_trusted(self.GOOD.replace("https://", "http://"), self.REPO)
        assert ok is False and "https" in reason.lower()

    def test_foreign_host_rejected(self):
        ok, _ = _package_url_trusted("https://evil.example.com/pkg.zip", self.REPO)
        assert ok is False

    def test_wrong_repo_rejected(self):
        ok, _ = _package_url_trusted(
            "https://github.com/attacker/evil/releases/download/v1/s247-function-app.zip", self.REPO
        )
        assert ok is False

    def test_custom_source_same_host_allowed(self):
        ok, _ = _package_url_trusted(
            "https://updates.mycorp.com/pkg.zip", "https://updates.mycorp.com/version.json"
        )
        assert ok is True

    def test_custom_source_cross_host_rejected(self):
        ok, _ = _package_url_trusted(
            "https://evil.example.com/pkg.zip", "https://updates.mycorp.com/version.json"
        )
        assert ok is False


class TestSha256OfFile:
    def test_known_digest(self, tmp_path):
        import hashlib
        f = tmp_path / "blob.bin"
        f.write_bytes(b"site24x7")
        assert _sha256_of_file(str(f)) == hashlib.sha256(b"site24x7").hexdigest()


# ─── check_and_apply_update ─────────────────────────────────────────────────


class TestCheckAndApplyUpdate:
    def test_no_update_url(self):
        result = check_and_apply_update()
        assert result["update_available"] is False
        assert "not configured" in result["message"]

    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_up_to_date(self, mock_local, mock_remote, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/version.json")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "1.0.0",
            "package_url": "https://example.com/v1.zip",
        }
        result = check_and_apply_update()
        assert result["update_available"] is False
        assert result["action"] == "up_to_date"

    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_update_available_no_auto(self, mock_local, mock_remote, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/version.json")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "2.0.0",
            "package_url": "https://example.com/v2.zip",
            "release_notes": "New stuff",
        }
        result = check_and_apply_update(auto_apply=False)
        assert result["update_available"] is True
        assert result["action"] == "update_available"
        assert result["package_url"] == "https://example.com/v2.zip"

    @patch("shared.updater.deploy_update")
    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_update_auto_apply(self, mock_local, mock_remote, mock_deploy, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/version.json")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "2.0.0",
            "package_url": "https://example.com/v2.zip",
        }
        mock_deploy.return_value = {"success": True, "status_code": 200}
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "deployed"
        mock_deploy.assert_called_once_with("https://example.com/v2.zip", expected_sha256=None)

    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_remote_fetch_fails(self, mock_local, mock_remote, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/version.json")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = None
        result = check_and_apply_update()
        assert result["update_available"] is False
        assert "Could not fetch" in result["message"]

    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_github_shorthand_url(self, mock_local, mock_remote, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "owner/repo")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "1.1.0",
            "package_url": "https://github.com/owner/repo/releases/download/v1.1.0/s247-function-app.zip",
        }
        result = check_and_apply_update()
        assert result["update_available"] is True
        # Verify fetch was called with the shorthand (it internally resolves)
        mock_remote.assert_called_once_with("owner/repo")


# ─── Safety hardening: PINNED_VERSION / SKIP / age gate / zip validation ────


class TestSkipAutoUpdate:
    def test_skip_via_env(self, monkeypatch):
        monkeypatch.setenv("SKIP_AUTO_UPDATE", "true")
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/version.json")
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "disabled"
        assert result["update_available"] is False


class TestPinnedVersion:
    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_pinned_matches_local(self, mock_local, mock_remote, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/v.json")
        monkeypatch.setenv("PINNED_VERSION", "1.0.0")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {"version": "2.0.0", "package_url": "x"}
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "pinned_current"
        assert result["update_available"] is False

    @patch("shared.updater.deploy_update")
    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_pinned_mismatch_with_remote(
        self, mock_local, mock_remote, mock_deploy, monkeypatch
    ):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/v.json")
        monkeypatch.setenv("PINNED_VERSION", "1.5.0")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {"version": "2.0.0", "package_url": "x"}
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "pinned_mismatch"
        mock_deploy.assert_not_called()

    @patch("shared.updater.deploy_update")
    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_pinned_matches_remote_deploys(
        self, mock_local, mock_remote, mock_deploy, monkeypatch
    ):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/v.json")
        monkeypatch.setenv("PINNED_VERSION", "1.5.0")
        monkeypatch.setenv("MIN_RELEASE_AGE_MINUTES", "0")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {"version": "1.5.0", "package_url": "pkg", "sha256": "abc"}
        mock_deploy.return_value = {"success": True}
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "deployed"
        mock_deploy.assert_called_once_with("pkg", expected_sha256="abc")


class TestReleaseAgeGate:
    @patch("shared.updater.deploy_update")
    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_young_release_deferred(
        self, mock_local, mock_remote, mock_deploy, monkeypatch
    ):
        from datetime import datetime, timezone
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/v.json")
        monkeypatch.setenv("MIN_RELEASE_AGE_MINUTES", "60")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "2.0.0",
            "package_url": "x",
            "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "release_too_young"
        mock_deploy.assert_not_called()

    @patch("shared.updater.deploy_update")
    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_old_release_deploys(
        self, mock_local, mock_remote, mock_deploy, monkeypatch
    ):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/v.json")
        monkeypatch.setenv("MIN_RELEASE_AGE_MINUTES", "60")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "2.0.0",
            "package_url": "pkg",
            "published_at": "2020-01-01T00:00:00Z",
        }
        mock_deploy.return_value = {"success": True}
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "deployed"


class TestValidateZipPackage:
    def _make_zip(self, tmp_path, files):
        import zipfile as zf
        p = tmp_path / "pkg.zip"
        with zf.ZipFile(p, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        return str(p)

    def test_valid_package(self, tmp_path):
        from shared.updater import validate_zip_package
        files = {
            "function-app/host.json": '{"version":"2.0"}',
            "function-app/requirements.txt": "azure-functions\n",
            "function-app/VERSION": "1.2.3",
            "function-app/AutoUpdater/__init__.py": "def main():\n    pass\n",
            "function-app/AutoUpdater/function.json": '{"bindings":[]}',
            "function-app/shared/updater.py": "x = 1\n",
        }
        ok, reason = validate_zip_package(self._make_zip(tmp_path, files))
        assert ok, reason

    def test_syntax_error_rejected(self, tmp_path):
        from shared.updater import validate_zip_package
        files = {
            "function-app/host.json": "{}",
            "function-app/requirements.txt": "",
            "function-app/VERSION": "1.0.0",
            "function-app/AutoUpdater/__init__.py": "def main(:\n",  # syntax error
            "function-app/AutoUpdater/function.json": "{}",
            "function-app/shared/updater.py": "x = 1\n",
        }
        ok, reason = validate_zip_package(self._make_zip(tmp_path, files))
        assert not ok
        assert "syntax" in reason.lower()

    def test_missing_required_file(self, tmp_path):
        from shared.updater import validate_zip_package
        files = {
            "function-app/host.json": "{}",
            "function-app/requirements.txt": "",
            # VERSION missing
            "function-app/AutoUpdater/__init__.py": "pass\n",
            "function-app/AutoUpdater/function.json": "{}",
            "function-app/shared/updater.py": "pass\n",
        }
        ok, reason = validate_zip_package(self._make_zip(tmp_path, files))
        assert not ok
        assert "missing" in reason.lower()

    def test_bad_json_rejected(self, tmp_path):
        from shared.updater import validate_zip_package
        files = {
            "function-app/host.json": "{not valid json",
            "function-app/requirements.txt": "",
            "function-app/VERSION": "1.0.0",
            "function-app/AutoUpdater/__init__.py": "pass\n",
            "function-app/AutoUpdater/function.json": "{}",
            "function-app/shared/updater.py": "pass\n",
        }
        ok, reason = validate_zip_package(self._make_zip(tmp_path, files))
        assert not ok
        assert "json" in reason.lower()

    def test_not_a_zip(self, tmp_path):
        from shared.updater import validate_zip_package
        p = tmp_path / "not.zip"
        p.write_bytes(b"hello")
        ok, reason = validate_zip_package(str(p))
        assert not ok


# ─── UPDATE_CHANNEL: stable vs prerelease ────────────────────────────────────


class TestUpdateChannel:
    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_stable_skips_prerelease_tag(self, mock_local, mock_remote, monkeypatch):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/v.json")
        monkeypatch.delenv("UPDATE_CHANNEL", raising=False)
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "1.1.0-alpha.1",
            "package_url": "x",
            "prerelease": False,  # even if flag missing, version string alone triggers it
        }
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "prerelease_skipped"
        assert result["update_available"] is False

    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_stable_skips_github_prerelease_flag(self, mock_local, mock_remote, monkeypatch):
        # Version string looks stable but GitHub release is flagged prerelease
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/v.json")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "1.1.0",
            "package_url": "x",
            "prerelease": True,
        }
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "prerelease_skipped"

    @patch("shared.updater.deploy_update")
    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_prerelease_channel_accepts_alpha(
        self, mock_local, mock_remote, mock_deploy, monkeypatch
    ):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/v.json")
        monkeypatch.setenv("UPDATE_CHANNEL", "prerelease")
        monkeypatch.setenv("MIN_RELEASE_AGE_MINUTES", "0")
        mock_local.return_value = "1.0.0"
        mock_remote.return_value = {
            "version": "1.1.0-alpha.3",
            "package_url": "pkg",
            "prerelease": True,
        }
        mock_deploy.return_value = {"success": True}
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "deployed"

    def test_resolve_prerelease_channel(self, monkeypatch):
        from shared.updater import _resolve_update_url
        monkeypatch.setenv("UPDATE_CHANNEL", "prerelease")
        url = _resolve_update_url("owner/repo")
        assert url.endswith("/releases?per_page=5")

    def test_resolve_stable_channel_default(self, monkeypatch):
        from shared.updater import _resolve_update_url
        monkeypatch.delenv("UPDATE_CHANNEL", raising=False)
        url = _resolve_update_url("owner/repo")
        assert url.endswith("/releases/latest")

    @patch("shared.updater.requests.get")
    def test_fetch_unwraps_list_response(self, mock_get):
        # /releases returns a list; we take the first (most recent)
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "tag_name": "v2.0.0-beta.1",
                "prerelease": True,
                "body": "",
                "assets": [
                    {
                        "name": "s247-function-app.zip",
                        "browser_download_url": "https://github.com/o/r/releases/download/v2.0.0-beta.1/s247-function-app.zip",
                    },
                ],
            },
            {"tag_name": "v1.5.0", "prerelease": False, "assets": []},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp
        result = fetch_remote_version("https://api.github.com/repos/o/r/releases?per_page=5")
        assert result["version"] == "2.0.0-beta.1"
        assert result["prerelease"] is True
