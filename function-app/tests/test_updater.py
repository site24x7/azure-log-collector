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
        mock_deploy.assert_called_once_with("https://example.com/v2.zip", expected_sha256=None, expected_version="2.0.0")

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
        mock_deploy.assert_called_once_with("pkg", expected_sha256="abc", expected_version="1.5.0")


    @patch("shared.updater.deploy_update")
    @patch("shared.updater.fetch_remote_version")
    @patch("shared.updater.get_local_version")
    def test_pin_older_than_installed_never_downgrades(
        self, mock_local, mock_remote, mock_deploy, monkeypatch
    ):
        monkeypatch.setenv("UPDATE_CHECK_URL", "https://example.com/v.json")
        monkeypatch.setenv("PINNED_VERSION", "1.0.0")
        monkeypatch.setenv("MIN_RELEASE_AGE_MINUTES", "0")
        mock_local.return_value = "1.5.0"
        mock_remote.return_value = {"version": "1.0.0", "package_url": "pkg", "sha256": "abc"}
        result = check_and_apply_update(auto_apply=True)
        assert result["action"] == "pinned_older"
        assert result["update_available"] is False
        mock_deploy.assert_not_called()


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


def test_package_version_must_match_the_advertised_version(tmp_path):
    """A manifest saying 2.0.0 cannot ship an older package past the downgrade check."""
    import zipfile
    from shared.updater import validate_zip_package
    pkg = tmp_path / "pkg.zip"
    with zipfile.ZipFile(pkg, "w") as zf:
        zf.writestr("host.json", "{}")
        zf.writestr("VERSION", "1.0.0")
        zf.writestr("AutoUpdater/__init__.py", "x = 1\n")
        zf.writestr("shared/updater.py", "x = 1\n")
        zf.writestr("requirements.txt", "")
        zf.writestr("AutoUpdater/function.json", "{}")
    ok_any, _ = validate_zip_package(str(pkg))
    assert ok_any
    ok_match, _ = validate_zip_package(str(pkg), expected_version="1.0.0")
    ok_other, reason = validate_zip_package(str(pkg), expected_version="2.0.0")
    assert (ok_match, ok_other) == (ok_any, False) and "does not match" in reason


class FakeStateStore:
    """In-memory update-state blob with ETag semantics (IfMatch / IfMissing)."""

    def __init__(self, state=None, fail_reads=0, fail_writes=0):
        self.text = json.dumps(state) if state is not None else None
        self.version = 1 if state is not None else 0
        self.fail_reads, self.fail_writes, self.writes = fail_reads, fail_writes, []

    def read(self, path, strict=False):
        if self.fail_reads:
            self.fail_reads -= 1
            raise RuntimeError("storage unavailable")
        return self.text, (f"etag-{self.version}" if self.text is not None else None)

    def write(self, path, data, etag):
        if self.fail_writes:
            self.fail_writes -= 1
            return False, False
        current = f"etag-{self.version}" if self.text is not None else None
        if etag != current:
            return False, True
        self.text, self.version = data, self.version + 1
        self.writes.append(json.loads(data))
        return True, False

    @property
    def state(self):
        return json.loads(self.text) if self.text else {}


def _store_patches(store):
    return (patch("shared.config_store._read_blob_with_etag", side_effect=store.read),
            patch("shared.config_store._write_blob_conditional", side_effect=store.write))


def _deploy(monkeypatch, tmp_path, put_status=202, store=None, put_error=None, on_put=None):
    import shared.updater as up
    monkeypatch.setenv("WEBSITE_SITE_NAME", "app")
    monkeypatch.setenv("SUBSCRIPTION_IDS", "sub")
    monkeypatch.setenv("RESOURCE_GROUP_NAME", "rg")
    monkeypatch.setenv("REQUIRE_PACKAGE_SHA256", "false")
    store = store or FakeStateStore()
    read_p, write_p = _store_patches(store)

    def fake_put(*a, **k):
        if on_put:
            on_put()
        if put_error:
            raise put_error
        return MagicMock(status_code=put_status, text="nope")

    with patch.object(up, "_package_url_trusted", return_value=(True, "")), \
         patch.object(up.requests, "get") as get, \
         patch.object(up, "validate_zip_package", return_value=(True, "")), \
         patch.object(up, "check_package_imports", return_value=(True, "")), \
         patch.object(up, "DefaultAzureCredential"), \
         patch.object(up.requests, "patch") as patch_call, \
         patch.object(up.requests, "put", side_effect=fake_put) as put, \
         patch.object(up, "_stage_verified_package", return_value="https://own.blob.core.windows.net/s247-packages/2.0.0-x/released-package.zip?sig") as stage, \
         patch.object(up, "get_local_version", return_value="1.0.0"), read_p, write_p:
        get.return_value.iter_content.return_value = [b"verified-bytes"]
        result = up.deploy_update("https://github.com/site24x7/azure-log-collector/releases/download/v2.0.0/s247-function-app.zip",
                                  expected_version="2.0.0")
    return result, put, patch_call, stage, store


def test_update_installs_the_verified_copy_with_onedeploy(monkeypatch, tmp_path):
    """Azure must install the bytes that were checked, not re-download the release URL."""
    store = FakeStateStore()
    seen = {}
    result, put, patch_call, stage, _ = _deploy(monkeypatch, tmp_path, store=store,
                                               on_put=lambda: seen.update(store.state))
    assert result["success"] and result["submitted"] and not result["confirmed"] and result["mode"] == "onedeploy"
    stage.assert_called_once()
    url = put.call_args.args[0]
    assert url.startswith("https://management.azure.com/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Web/sites/app/extensions/onedeploy?")
    assert put.call_args.kwargs["json"] == {"properties": {
        "packageUri": "https://own.blob.core.windows.net/s247-packages/2.0.0-x/released-package.zip?sig", "remoteBuild": False}}
    patch_call.assert_not_called()          # Flex: no app-setting swap, no restart call
    # The attempt is recorded before the deploy, which restarts this app.
    assert seen["pending_version"] == "2.0.0" and seen["attempt_id"] == result["attempt_id"]


def test_rejected_onedeploy_records_the_failure(monkeypatch, tmp_path):
    result, _, _, _, store = _deploy(monkeypatch, tmp_path, put_status=409)
    assert result["success"] is False and result["status_code"] == 409
    assert store.state["failed_at"] and "HTTP 409" in store.state["error"]


def test_a_lost_deploy_request_is_recorded_and_does_not_block_a_retry(monkeypatch, tmp_path):
    import requests
    result, _, _, _, store = _deploy(monkeypatch, tmp_path, put_error=requests.ConnectionError("timeout"))
    assert result["success"] is False and "ConnectionError" in result["error"]
    assert store.state["failed_at"] and "may still complete" in store.state["error"]
    retry, put, *_ = _deploy(monkeypatch, tmp_path, store=store)
    assert retry["success"] is True and put.call_count == 1


def test_nothing_is_installed_when_the_state_cannot_be_recorded(monkeypatch, tmp_path):
    result, put, *_ = _deploy(monkeypatch, tmp_path, store=FakeStateStore(fail_writes=1))
    assert result["success"] is False and "update state" in result["error"]
    put.assert_not_called()


def test_nothing_is_installed_when_the_state_cannot_be_read(monkeypatch, tmp_path):
    result, put, *_ = _deploy(monkeypatch, tmp_path, store=FakeStateStore(fail_reads=1))
    assert result["success"] is False and "update state" in result["error"]
    put.assert_not_called()


def test_a_second_update_waits_for_the_one_in_progress(monkeypatch, tmp_path):
    from datetime import datetime, timezone
    recent = {"pending_version": "1.9.0", "attempt_id": "a", "submitted_at": datetime.now(timezone.utc).isoformat()}
    result, put, _, stage, store = _deploy(monkeypatch, tmp_path, store=FakeStateStore(recent))
    assert result["success"] is False and result["in_progress"] and "1.9.0" in result["error"]
    put.assert_not_called() and stage.assert_not_called()
    assert store.state == recent


def test_simultaneous_updates_install_only_once(monkeypatch, tmp_path):
    # Another request claims the state between this one's read and its claim.
    import shared.updater as up
    store = FakeStateStore()
    real_stage = MagicMock(return_value="https://own/x/released-package.zip?sig")

    def stage_and_race(*a, **k):
        store.write("update-state.json", json.dumps({"attempt_id": "other", "pending_version": "2.0.0",
                                                     "submitted_at": "2099-01-01T00:00:00+00:00"}), None)
        return real_stage()

    with patch.object(up, "_stage_verified_package", side_effect=stage_and_race):
        monkeypatch.setenv("WEBSITE_SITE_NAME", "app")
        monkeypatch.setenv("SUBSCRIPTION_IDS", "sub")
        monkeypatch.setenv("REQUIRE_PACKAGE_SHA256", "false")
        read_p, write_p = _store_patches(store)
        with patch.object(up, "_package_url_trusted", return_value=(True, "")), \
             patch.object(up.requests, "get") as get, \
             patch.object(up, "validate_zip_package", return_value=(True, "")), \
             patch.object(up, "check_package_imports", return_value=(True, "")), \
             patch.object(up, "DefaultAzureCredential"), \
             patch.object(up.requests, "put") as put, read_p, write_p:
            get.return_value.iter_content.return_value = [b"zip"]
            result = up.deploy_update("https://github.com/site24x7/azure-log-collector/releases/download/v2.0.0/s247-function-app.zip")
    assert result["success"] is False and result["in_progress"]
    put.assert_not_called()
    assert store.state["attempt_id"] == "other"


@pytest.mark.parametrize("state", [
    {"pending_version": "1.9.0", "submitted_at": "2020-01-01T00:00:00+00:00"},            # stale
    {"pending_version": "1.9.0", "submitted_at": "2099-01-01T00:00:00+00:00", "confirmed_at": "x"},
    {"pending_version": "1.9.0", "submitted_at": "2099-01-01T00:00:00+00:00", "failed_at": "x"},
])
def test_finished_or_stale_updates_do_not_block(monkeypatch, tmp_path, state):
    result, put, *_ = _deploy(monkeypatch, tmp_path, store=FakeStateStore(state))
    assert result["success"] is True
    put.assert_called_once()


def test_a_late_failure_does_not_overwrite_a_newer_attempt(monkeypatch):
    import shared.updater as up
    store = FakeStateStore({"attempt_id": "newer", "pending_version": "2.1.0"})
    read_p, write_p = _store_patches(store)
    with read_p, write_p:
        up._record_failed_attempt({"attempt_id": "older"}, "boom")
    assert "failed_at" not in store.state and store.state["attempt_id"] == "newer"


def _confirm(monkeypatch, store, local="2.0.0", runs=2):
    import shared.updater as up
    monkeypatch.setattr(up, "_confirmation_checked", False)
    read_p, write_p = _store_patches(store)
    with read_p, write_p, patch.object(up, "get_local_version", return_value=local):
        for _ in range(runs):
            up.confirm_running_version()
    return up


def test_new_version_confirms_the_update_once(monkeypatch):
    store = FakeStateStore({"pending_version": "2.0.0", "failed_at": "x", "error": "lost"})
    _confirm(monkeypatch, store)
    assert len(store.writes) == 1 and store.state["confirmed_at"]
    assert "failed_at" not in store.state and "error" not in store.state


def test_old_version_does_not_confirm(monkeypatch):
    store = FakeStateStore({"pending_version": "2.0.0"})
    _confirm(monkeypatch, store, local="1.0.0")
    assert store.writes == []


@pytest.mark.parametrize("failure", ["unreadable", "unwritable"])
def test_confirmation_is_retried_after_a_storage_failure(monkeypatch, failure):
    store = FakeStateStore({"pending_version": "2.0.0"},
                           fail_reads=1 if failure == "unreadable" else 0,
                           fail_writes=1 if failure == "unwritable" else 0)
    up = _confirm(monkeypatch, store)
    assert store.state["confirmed_at"] and up._confirmation_checked is True


def test_staged_package_link_is_read_only_https(monkeypatch, tmp_path):
    import shared.updater as up
    pkg = tmp_path / "pkg.zip"
    pkg.write_bytes(b"zip")
    monkeypatch.setenv("AzureWebJobsStorage", "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=a2V5;EndpointSuffix=core.windows.net")
    with patch("azure.storage.blob.BlobClient.upload_blob") as upload, \
         patch("azure.storage.blob.ContainerClient.create_container"):
        link = up._stage_verified_package(str(pkg), "2.0.0", "ab" * 32)
    upload.assert_called_once()
    assert link.startswith("https://acct.blob.core.windows.net/s247-packages/2.0.0-abababababababab/released-package.zip?")
    assert "sp=r&" in link and "spr=https" in link


def _package(tmp_path, files, prefix=""):
    import zipfile
    path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(prefix + name, body)
    return str(path)


GOOD_FILES = {
    "host.json": "{}",
    "HealthCheck/function.json": "{}",
    "HealthCheck/__init__.py": "import json\nimport shared.helper\ndef main(req):\n    return 1\n",
    "shared/__init__.py": "",
    "shared/helper.py": "import s247dep\n",
    ".python_packages/lib/site-packages/s247dep/__init__.py": "VALUE = 1\n",
}


@pytest.mark.parametrize("prefix", ["", "function-app/"])
def test_package_that_imports_cleanly_passes(tmp_path, prefix):
    from shared.updater import check_package_imports
    assert check_package_imports(_package(tmp_path, GOOD_FILES, prefix)) == (True, "")


def test_package_with_a_broken_function_is_refused(tmp_path):
    from shared.updater import check_package_imports
    files = dict(GOOD_FILES, **{"Broken/function.json": "{}", "Broken/__init__.py": "from shared.helper import missing_name\n"})
    ok, reason = check_package_imports(_package(tmp_path, files))
    assert not ok and reason.startswith("Broken cannot be imported: ImportError")


def test_package_missing_a_dependency_is_refused(tmp_path):
    # A shared module's dependency is checked even when no function imports it at load time.
    from shared.updater import check_package_imports
    files = {k: v for k, v in GOOD_FILES.items() if "s247dep" not in k}
    files["HealthCheck/__init__.py"] = "def main(req):\n    return 1\n"
    ok, reason = check_package_imports(_package(tmp_path, files))
    assert not ok and reason.startswith("shared.helper cannot be imported: ModuleNotFoundError")


def test_package_does_not_see_this_apps_modules(tmp_path):
    # The check must use only the package's own code and dependencies.
    from shared.updater import check_package_imports
    files = dict(GOOD_FILES, **{"shared/helper.py": "import azure.functions\n"})
    ok, reason = check_package_imports(_package(tmp_path, files))
    assert not ok and "azure" in reason


def test_package_with_an_escaping_path_is_refused(tmp_path):
    from shared.updater import check_package_imports
    ok, reason = check_package_imports(_package(tmp_path, dict(GOOD_FILES, **{"../evil.py": "x = 1"})))
    assert not ok and reason.startswith("unsafe path")
    assert not (tmp_path.parent / "evil.py").exists()


def test_deploy_refuses_a_package_that_does_not_import(monkeypatch, tmp_path):
    import shared.updater as up
    monkeypatch.setenv("WEBSITE_SITE_NAME", "app")
    monkeypatch.setenv("SUBSCRIPTION_IDS", "sub")
    monkeypatch.setenv("REQUIRE_PACKAGE_SHA256", "false")
    with patch.object(up, "_package_url_trusted", return_value=(True, "")), \
         patch.object(up.requests, "get") as get, \
         patch.object(up, "validate_zip_package", return_value=(True, "")), \
         patch.object(up, "check_package_imports", return_value=(False, "X cannot be imported: ImportError: y")), \
         patch.object(up, "_stage_verified_package") as stage, \
         patch.object(up.requests, "put") as put:
        get.return_value.iter_content.return_value = [b"zip"]
        result = up.deploy_update("https://github.com/site24x7/azure-log-collector/releases/download/v2.0.0/s247-function-app.zip")
    assert result["success"] is False and result["validation_failed"] and "import check" in result["error"]
    stage.assert_not_called() and put.assert_not_called()
