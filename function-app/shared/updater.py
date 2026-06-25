"""Self-update logic for the Function App.

Checks a remote endpoint for newer versions and deploys updates
automatically using the Azure Management API.

Supports two URL formats for UPDATE_CHECK_URL:

1. **Direct version.json** — a JSON file with:
   ``{"version": "1.1.0", "package_url": "https://...", "release_notes": "..."}``

2. **GitHub Releases API** — the ``/releases/latest`` endpoint, e.g.
   ``https://api.github.com/repos/owner/repo/releases/latest``
   The release tag must be ``vX.Y.Z`` and have a ``s247-function-app.zip``
   asset attached.  A ``version.json`` asset is also accepted.

3. **GitHub shorthand** — just ``owner/repo`` (e.g. ``site24x7/azure-log-collector``).
   Automatically expanded to the GitHub Releases latest API URL.
"""

import ast
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def get_local_version() -> str:
    """Read the current version from the VERSION file."""
    try:
        return VERSION_FILE.read_text().strip()
    except Exception:
        return "0.0.0"


def parse_version(version_str: str) -> Tuple[int, ...]:
    """Parse a semver string into a comparable tuple.

    Handles pre-release suffixes so that:
      * ``0.1.0-alpha.2`` < ``0.1.0-alpha.3``
      * ``0.1.0-alpha.9`` < ``0.1.0-beta.1`` (alpha < beta per semver)
      * ``0.1.0-beta.5`` < ``0.1.0-rc.1``   (beta < rc)
      * ``0.1.0-rc.3``   < ``0.1.0``        (release beats pre-release)

    Returns a 6-tuple ``(major, minor, patch, stability, label_rank, pre_num)``
    where:
      * stability: 0 = pre-release, 1 = full release
      * label_rank: 0 for dev/unknown, 1 alpha, 2 beta, 3 rc (0 when
        stability=1 since full release has no label)
      * pre_num: numeric suffix of the pre-release identifier (0 if absent)

    For "0.1.0-alpha.3" → (0, 1, 0, 0, 1, 3)
    For "1.2.3"         → (1, 2, 3, 1, 0, 0)
    """
    _LABEL_RANK = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3}
    try:
        stripped = version_str.strip().lstrip("v")
        parts = stripped.split("-", 1)
        core = tuple(int(x) for x in parts[0].split("."))
        if len(parts) == 1:
            # No pre-release — full release sorts higher.
            return core + (1, 0, 0)
        # Pre-release suffix: extract alphabetic label (alpha/beta/rc/…)
        # AND numeric suffix.  Segments can be "alpha" + "3" (dot-separated)
        # or "alpha3" (fused), so scan every dot-separated segment.
        pre = parts[1]
        label = ""
        pre_num = 0
        for seg in pre.split("."):
            if seg.isdigit():
                pre_num = int(seg)
                continue
            m = re.match(r"^([A-Za-z]+)(\d+)?$", seg)
            if m:
                if not label:
                    label = m.group(1).lower()
                if m.group(2) is not None:
                    pre_num = int(m.group(2))
        rank = _LABEL_RANK.get(label, 0)
        return core + (0, rank, pre_num)
    except (ValueError, AttributeError):
        return (0, 0, 0, 0, 0, 0)


def _resolve_update_url(raw_url: str) -> str:
    """Resolve shorthand ``owner/repo`` to full GitHub API URL.

    If the URL is already a full HTTP URL it is returned as-is.
    """
    raw_url = raw_url.strip()
    if raw_url.startswith(("http://", "https://")):
        return raw_url
    # Treat as GitHub owner/repo shorthand. Channel controls which endpoint:
    #   stable     → /releases/latest (GitHub auto-excludes pre-releases)
    #   prerelease → /releases        (returns all, we take the first)
    if re.match(r"^[\w.-]+/[\w.-]+$", raw_url):
        channel = os.environ.get("UPDATE_CHANNEL", "stable").strip().lower()
        if channel == "prerelease":
            return f"https://api.github.com/repos/{raw_url}/releases?per_page=5"
        return f"https://api.github.com/repos/{raw_url}/releases/latest"
    return raw_url


def _parse_github_release(data: Dict) -> Optional[Dict]:
    """Extract version info from a GitHub Releases API response."""
    tag = data.get("tag_name", "")
    version = tag.lstrip("v")
    if not version:
        return None

    assets: List[Dict] = data.get("assets", [])
    published_at = data.get("published_at") or data.get("created_at") or ""
    is_prerelease = bool(data.get("prerelease", False))

    # Look for version.json asset first (has explicit package_url)
    for asset in assets:
        if asset.get("name") == "version.json":
            try:
                dl_url = asset.get("browser_download_url", "")
                resp = requests.get(dl_url, timeout=30)
                resp.raise_for_status()
                info = resp.json()
                info.setdefault("published_at", published_at)
                info.setdefault("prerelease", is_prerelease)
                return info
            except Exception:
                logger.debug("Could not download version.json asset — falling back to zip detection")

    # Fall back to finding the zip asset directly
    zip_url = None
    for asset in assets:
        name = asset.get("name", "")
        if name.endswith(".zip") and "function-app" in name.lower():
            zip_url = asset.get("browser_download_url")
            break

    if not zip_url:
        # Accept any .zip asset
        for asset in assets:
            if asset.get("name", "").endswith(".zip"):
                zip_url = asset.get("browser_download_url")
                break

    if not zip_url:
        logger.error("GitHub release %s has no zip asset", tag)
        return None

    return {
        "version": version,
        "package_url": zip_url,
        "release_notes": data.get("body", ""),
        "published_at": published_at,
        "prerelease": is_prerelease,
    }


def fetch_remote_version(update_url: str) -> Optional[Dict]:
    """Fetch the remote version info from UPDATE_CHECK_URL.

    Supports both direct ``version.json`` and GitHub Releases API responses.
    Returns dict with ``version``, ``package_url``, ``release_notes`` or None.
    """
    resolved_url = _resolve_update_url(update_url)

    try:
        headers = {"Accept": "application/json"}
        # Add GitHub API header if it's a GitHub URL
        if "api.github.com" in resolved_url:
            headers["Accept"] = "application/vnd.github+json"
            gh_token = os.environ.get("GITHUB_TOKEN", "")
            if gh_token:
                headers["Authorization"] = f"Bearer {gh_token}"

        resp = requests.get(resolved_url, timeout=30, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        # /releases returns a list. We take the most recent — GitHub sorts DESC.
        # On the prerelease channel this naturally picks up alphas/betas.
        if isinstance(data, list):
            if not data:
                logger.error("Releases list from %s is empty", resolved_url)
                return None
            data = data[0]

        # Detect GitHub Releases API response (has tag_name field)
        if "tag_name" in data:
            return _parse_github_release(data)

        # Direct version.json format
        if "version" not in data or "package_url" not in data:
            logger.error("Remote version.json missing 'version' or 'package_url'")
            return None
        return data

    except Exception as e:
        logger.error("Failed to fetch remote version from %s: %s", resolved_url, e)
        return None


def is_update_available(local_version: str, remote_version: str) -> bool:
    """Compare versions — returns True if remote is newer."""
    return parse_version(remote_version) > parse_version(local_version)


# ─── Supply-chain integrity (authenticity of the downloaded package) ─────────

# Hosts that legitimately serve GitHub release assets. github.com issues the
# browser_download_url; it 302-redirects to the *.githubusercontent.com CDN.
_GITHUB_RELEASE_HOSTS = frozenset({
    "github.com",
    "www.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})


def _sha256_of_file(path: str) -> str:
    """Stream a file through SHA-256 and return the lowercase hex digest."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _package_url_trusted(package_url: str, update_url: str) -> Tuple[bool, str]:
    """Decide whether ``package_url`` is safe to download and deploy.

    The AutoUpdater deploys whatever this URL serves as live code running under
    the Function App's managed identity, so the URL must be constrained.

    Enforced unconditionally:
      - **HTTPS only** — a plaintext package_url is MITM-able into RCE.

    Host policy, by configured update source (``UPDATE_CHECK_URL``):
      - **GitHub source** (``owner/repo`` shorthand or a github.com/api.github.com
        URL): package_url must be on a GitHub release host. For the shorthand
        form the owner/repo must also appear in the package_url path, so a
        tampered version.json cannot redirect to a *different* GitHub repo.
      - **Custom source**: package_url must share the update source's host — a
        custom version.json may only serve packages from its own origin.

    Returns ``(ok, reason)``; ``reason`` is empty on success.
    """
    pkg = urlparse(package_url.strip())
    if pkg.scheme != "https":
        return False, f"package_url must use https (got '{pkg.scheme or 'no scheme'}')"
    pkg_host = (pkg.hostname or "").lower()
    if not pkg_host:
        return False, "package_url has no host"

    src = (update_url or "").strip()
    if re.match(r"^[\w.-]+/[\w.-]+$", src):  # owner/repo shorthand
        if pkg_host not in _GITHUB_RELEASE_HOSTS:
            return False, f"package_url host '{pkg_host}' is not a GitHub release host"
        owner_repo = src.lower()
        if pkg_host in ("github.com", "www.github.com") and f"/{owner_repo}/" not in pkg.path.lower():
            return False, f"package_url path does not belong to update repo '{owner_repo}'"
        return True, ""

    src_host = (urlparse(src).hostname or "").lower()
    if src_host.endswith("github.com"):
        if pkg_host not in _GITHUB_RELEASE_HOSTS:
            return False, f"package_url host '{pkg_host}' is not a GitHub release host"
        return True, ""

    # Fully custom source: package must come from the same origin as version.json.
    if src_host and pkg_host != src_host:
        return False, (
            f"package_url host '{pkg_host}' does not match update source host '{src_host}'"
        )
    return True, ""


# ─── Safety helpers (prevent AutoUpdater from deploying a broken build) ──────

# Critical files that must be present and parseable. AutoUpdater refuses to
# deploy a zip missing any of these — that would brick the Function App.
_REQUIRED_FILES = (
    "function-app/host.json",
    "function-app/requirements.txt",
    "function-app/VERSION",
    "function-app/AutoUpdater/__init__.py",
    "function-app/AutoUpdater/function.json",
    "function-app/shared/updater.py",
)


def _release_too_young(published_at: str, min_age_minutes: int) -> bool:
    """True if the release is younger than ``min_age_minutes``.

    Lets ops notice and delete a bad release before it auto-propagates.
    """
    if not published_at or min_age_minutes <= 0:
        return False
    try:
        pub = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except Exception:
        return False
    age = (datetime.now(timezone.utc) - pub).total_seconds() / 60.0
    return age < min_age_minutes


def validate_zip_package(zip_path: str) -> Tuple[bool, str]:
    """Validate a downloaded update zip before deploying it.

    Refuses anything with:
      - syntax errors in any .py file
      - invalid JSON in host.json / function.json
      - missing critical files (host.json, VERSION, AutoUpdater, shared/updater.py)

    Returns ``(ok, reason)``. ``reason`` is empty on success.
    """
    try:
        if not zipfile.is_zipfile(zip_path):
            return False, "downloaded file is not a valid zip archive"

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()

            # Some release zips are rooted at function-app/, others at the
            # repo root. Detect the prefix.
            prefix = ""
            if not any(n.startswith("function-app/") for n in names):
                # Maybe rooted at function-app contents directly
                if "host.json" in names:
                    prefix = ""
                    required = tuple(
                        r.replace("function-app/", "") for r in _REQUIRED_FILES
                    )
                else:
                    return False, "zip layout unrecognised (no host.json found)"
            else:
                required = _REQUIRED_FILES

            missing = [r for r in required if r not in names]
            if missing:
                return False, f"missing required files: {missing[:3]}"

            # Syntax-check every .py (app code only; skip vendored deps),
            # JSON-validate every .json
            py_count = 0
            json_count = 0
            for name in names:
                if name.endswith("/") or "__pycache__" in name:
                    continue
                if name.endswith(".py"):
                    # Skip vendored third-party packages (pip-installed into
                    # .python_packages/). Those are presumed valid — they came
                    # from PyPI, and they occasionally contain BOMs / encoding
                    # quirks that our strict ast.parse check rejects even
                    # though Python's own loader handles them fine.
                    if ".python_packages/" in name or name.startswith(".python_packages/"):
                        continue
                    py_count += 1
                    try:
                        # utf-8-sig strips a leading BOM if present; the
                        # Python parser itself tolerates BOM, but decoding
                        # as plain utf-8 leaves \ufeff in the source which
                        # ast.parse then rejects.
                        src = zf.read(name).decode("utf-8-sig", errors="replace")
                        ast.parse(src, filename=name)
                    except SyntaxError as e:
                        return False, f"syntax error in {name}: {e}"
                    except Exception as e:
                        return False, f"cannot parse {name}: {e}"
                elif name.endswith((".json",)):
                    # host.json, function.json, settings files
                    json_count += 1
                    try:
                        json.loads(zf.read(name).decode("utf-8", errors="replace"))
                    except Exception as e:
                        return False, f"invalid JSON in {name}: {e}"

            # VERSION file must exist and not be empty
            version_path = f"{prefix}VERSION" if prefix == "" else "function-app/VERSION"
            # Try both possible paths
            for p in ("function-app/VERSION", "VERSION"):
                if p in names:
                    version_path = p
                    break
            try:
                v = zf.read(version_path).decode("utf-8").strip()
                if not v or len(v) > 64:
                    return False, "VERSION file empty or absurdly long"
            except Exception as e:
                return False, f"cannot read VERSION: {e}"

            logger.info(
                "Update zip validated: %d py files, %d json files, VERSION=%s",
                py_count, json_count, v,
            )
            return True, ""
    except Exception as e:
        return False, f"validation crashed: {e}"


def _post_deploy_health_check(func_app_name: str, checks: int = 3,
                               interval_sec: int = 30) -> Dict:
    """Ping our own /api/health a few times post-deploy and return the result.

    Informational only — does NOT trigger a rollback. Gives ops visibility
    (via debug_logger audit trail) into whether the newly-deployed build
    actually starts.
    """
    hostname = f"{func_app_name}.azurewebsites.net"
    url = f"https://{hostname}/api/health"
    # Warm-up: newly-deployed Functions need time to cold-start + load deps
    time.sleep(20)

    results = []
    for i in range(checks):
        try:
            resp = requests.get(url, timeout=15)
            results.append({"status": resp.status_code, "ok": resp.ok})
            if resp.ok:
                return {"healthy": True, "checks": results, "url": url}
        except Exception as e:
            results.append({"error": str(e)[:200]})
        if i < checks - 1:
            time.sleep(interval_sec)

    return {"healthy": False, "checks": results, "url": url}


def deploy_update(package_url: str, expected_sha256: Optional[str] = None) -> Dict:
    """Download the package and deploy it to this Function App.

    The customer-facing one-click ARM template deploys the Function App with
    ``WEBSITE_RUN_FROM_PACKAGE`` pointing at a release zip URL. In that mode the
    correct way to push a new version is to update that app setting to the new
    URL and restart — zipdeploy is ignored when run-from-package-URL is active.

    For dev deploys (set up via ``setup.sh``) the URL setting is cleared in
    favour of Oryx build, so we fall back to the classic zipdeploy path.

    Before anything is deployed the package passes three gates: (1) the URL must
    be HTTPS and from a trusted host, (2) its SHA-256 must match the digest
    published in ``version.json`` (unless ``REQUIRE_PACKAGE_SHA256=false``), and
    (3) the zip must be structurally valid. Any failure refuses the deploy.
    """
    resource_group = os.environ.get(
        "RESOURCE_GROUP_NAME", os.environ.get("RESOURCE_GROUP", "s247-diag-logs-rg")
    )
    func_app_name = os.environ.get("WEBSITE_SITE_NAME", "")
    sub_id = os.environ.get("SUBSCRIPTION_IDS", "").split(",")[0].strip()

    if not func_app_name:
        return {"success": False, "error": "WEBSITE_SITE_NAME not set"}
    if not sub_id:
        return {"success": False, "error": "No subscription ID available"}

    # ── Gate 1: only ever fetch/deploy from a trusted, HTTPS package URL.
    ok, reason = _package_url_trusted(package_url, os.environ.get("UPDATE_CHECK_URL", ""))
    if not ok:
        logger.error("Refusing to deploy — untrusted package_url: %s", reason)
        return {"success": False, "error": f"untrusted package_url: {reason}",
                "integrity_failed": True}

    # ── Gate 2 (pre-flight): refuse if no digest is published and we require one.
    require_sha = os.environ.get("REQUIRE_PACKAGE_SHA256", "true").lower() in ("1", "true", "yes")
    if require_sha and not expected_sha256:
        logger.error("Refusing to deploy — release has no sha256 digest and "
                     "REQUIRE_PACKAGE_SHA256 is enabled")
        return {"success": False,
                "error": "release published no sha256 digest "
                         "(set REQUIRE_PACKAGE_SHA256=false to override)",
                "integrity_failed": True}

    tmp_path = None
    try:
        # Download once — reused for the integrity check, structural validation,
        # and (in Oryx/zipdeploy mode) the actual upload.
        logger.info(f"Downloading update from {package_url} ...")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            resp = requests.get(package_url, timeout=300, stream=True)
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=8192):
                tmp.write(chunk)
            tmp_path = tmp.name
        logger.info(f"Downloaded to {tmp_path}")

        # ── Gate 2 (verify): the bytes must match the published digest.
        if expected_sha256:
            actual = _sha256_of_file(tmp_path)
            if actual != expected_sha256.strip().lower():
                logger.error("Refusing to deploy — sha256 mismatch "
                             "(expected %s, got %s)", expected_sha256, actual)
                return {"success": False,
                        "error": "package sha256 mismatch — possible tampering",
                        "integrity_failed": True}
            logger.info("Package integrity verified (sha256 %s)", actual)

        # ── Gate 3: never deploy a zip that won't import / parse.
        # This is what prevents AutoUpdater from bricking itself.
        ok, reason = validate_zip_package(tmp_path)
        if not ok:
            logger.error("Refusing to deploy — package validation failed: %s", reason)
            return {
                "success": False,
                "error": f"package validation failed: {reason}",
                "validation_failed": True,
            }

        credential = DefaultAzureCredential()
        token = credential.get_token("https://management.azure.com/.default")
        site_base = (
            f"https://management.azure.com/subscriptions/{sub_id}"
            f"/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Web/sites/{func_app_name}"
        )
        auth_headers = {"Authorization": f"Bearer {token.token}"}

        # Detect deployment mode — URL-based run-from-package vs Oryx build.
        settings_resp = requests.post(
            f"{site_base}/config/appsettings/list?api-version=2023-01-01",
            headers={**auth_headers, "Content-Length": "0"},
            timeout=30,
        )
        current_pkg_url = ""
        if settings_resp.ok:
            current_pkg_url = (
                settings_resp.json().get("properties", {}).get("WEBSITE_RUN_FROM_PACKAGE", "")
            )

        if current_pkg_url and current_pkg_url.lower().startswith(("http://", "https://")):
            logger.info(
                "Run-from-package (URL) mode detected — updating WEBSITE_RUN_FROM_PACKAGE"
            )
            patch_resp = requests.patch(
                f"{site_base}/config/appsettings?api-version=2023-01-01",
                headers={**auth_headers, "Content-Type": "application/json"},
                json={"properties": {**settings_resp.json().get("properties", {}),
                                     "WEBSITE_RUN_FROM_PACKAGE": package_url}},
                timeout=60,
            )
            if not patch_resp.ok:
                error_msg = patch_resp.text[:500]
                logger.error(
                    f"App setting update failed (HTTP {patch_resp.status_code}): {error_msg}"
                )
                return {
                    "success": False,
                    "status_code": patch_resp.status_code,
                    "error": error_msg,
                }
            # Restart the Function App so it picks up the new package
            restart_resp = requests.post(
                f"{site_base}/restart?api-version=2023-01-01",
                headers={**auth_headers, "Content-Length": "0"},
                timeout=60,
            )
            if restart_resp.status_code not in (200, 204):
                logger.warning(
                    "Restart returned HTTP %s — new package will be picked up on next "
                    "worker recycle",
                    restart_resp.status_code,
                )
            logger.info("Update deployed successfully (run-from-package URL swap)")
            return {"success": True, "status_code": patch_resp.status_code, "mode": "run-from-package"}

        # Fallback: zipdeploy (dev / Oryx build mode) — reuse the already
        # downloaded + integrity-verified package; don't fetch it twice.
        logger.info("Oryx build mode detected — using zipdeploy")
        with open(tmp_path, "rb") as f:
            deploy_resp = requests.post(
                f"{site_base}/extensions/zipdeploy?api-version=2023-01-01",
                headers={**auth_headers, "Content-Type": "application/octet-stream"},
                data=f,
                timeout=600,
            )

        if deploy_resp.status_code in (200, 202):
            logger.info("Update deployed successfully (zipdeploy)")
            return {"success": True, "status_code": deploy_resp.status_code, "mode": "zipdeploy"}
        else:
            error_msg = deploy_resp.text[:500]
            logger.error(
                f"Deploy failed (HTTP {deploy_resp.status_code}): {error_msg}"
            )
            return {
                "success": False,
                "status_code": deploy_resp.status_code,
                "error": error_msg,
            }

    except Exception as e:
        logger.error(f"Update deployment failed: {e}")
        return {"success": False, "error": str(e)}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def check_and_apply_update(auto_apply: bool = False) -> Dict:
    """Full update check workflow.

    Args:
        auto_apply: If True, automatically deploy the update.
                    If False, only report availability.

    Returns:
        Status dict with update info and action taken.
    """
    local_ver = get_local_version()

    # ── Emergency kill switch: ops can disable AutoUpdater without a redeploy.
    if os.environ.get("SKIP_AUTO_UPDATE", "").lower() in ("1", "true", "yes"):
        return {
            "update_available": False,
            "message": "SKIP_AUTO_UPDATE is set — updates disabled by operator",
            "local_version": local_ver,
            "action": "disabled",
        }

    update_url = os.environ.get("UPDATE_CHECK_URL", "")
    if not update_url:
        return {
            "update_available": False,
            "message": "UPDATE_CHECK_URL not configured — auto-updates disabled",
            "local_version": local_ver,
        }

    remote_info = fetch_remote_version(update_url)

    if not remote_info:
        return {
            "update_available": False,
            "message": "Could not fetch remote version info",
            "local_version": local_ver,
        }

    remote_ver = remote_info["version"]
    has_update = is_update_available(local_ver, remote_ver)

    result = {
        "update_available": has_update,
        "local_version": local_ver,
        "remote_version": remote_ver,
        "release_notes": remote_info.get("release_notes", ""),
    }

    # ── Channel guard: customers on the 'stable' channel never install
    # alpha/beta/rc builds, even if misconfiguration points them at a
    # prerelease URL. Your test environment sets UPDATE_CHANNEL=prerelease.
    channel = os.environ.get("UPDATE_CHANNEL", "stable").strip().lower()
    # A version is pre-release if parse_version's stability slot (index -3
    # in the new 6-tuple: major, minor, patch, stability, label_rank, pre_num)
    # is 0, OR the GitHub release is flagged.
    parsed = parse_version(remote_ver)
    is_prerelease = bool(remote_info.get("prerelease")) or (
        len(parsed) >= 3 and parsed[-3] == 0 and parsed[:-3] != (0, 0, 0)
    )
    if is_prerelease and channel != "prerelease":
        result["action"] = "prerelease_skipped"
        result["update_available"] = False
        result["channel"] = channel
        result["message"] = (
            f"Remote {remote_ver} is a pre-release; channel={channel} — skipping"
        )
        return result
    result["channel"] = channel

    # ── Pinned version: stick to exactly this version, nothing else.
    # Use for freezing prod or rolling back to a specific known-good release.
    pinned = os.environ.get("PINNED_VERSION", "").strip().lstrip("v")
    if pinned:
        result["pinned_version"] = pinned
        if parse_version(pinned) == parse_version(local_ver):
            result["action"] = "pinned_current"
            result["update_available"] = False
            result["message"] = f"PINNED_VERSION={pinned} already installed"
            return result
        # Pinned differs from local — only deploy if remote matches the pin.
        if parse_version(remote_ver) != parse_version(pinned):
            result["action"] = "pinned_mismatch"
            result["update_available"] = False
            result["message"] = (
                f"PINNED_VERSION={pinned} but remote latest is {remote_ver} — skipping"
            )
            return result
        # Remote == pinned && != local → proceed (treat as update).
        has_update = True
        result["update_available"] = True

    # ── Minimum release age: skip releases younger than N minutes.
    # Gives ops time to delete a bad release before it auto-propagates.
    if has_update and auto_apply:
        try:
            min_age = int(os.environ.get("MIN_RELEASE_AGE_MINUTES", "60"))
        except ValueError:
            min_age = 60
        published_at = remote_info.get("published_at", "")
        if _release_too_young(published_at, min_age):
            result["action"] = "release_too_young"
            result["message"] = (
                f"Release {remote_ver} is younger than {min_age}m — deferring"
            )
            result["published_at"] = published_at
            return result

    if has_update and auto_apply:
        logger.info(f"Applying update: {local_ver} → {remote_ver}")
        deploy_result = deploy_update(
            remote_info["package_url"], expected_sha256=remote_info.get("sha256")
        )
        result["deploy_result"] = deploy_result
        result["action"] = "deployed" if deploy_result["success"] else "deploy_failed"
    elif has_update:
        result["action"] = "update_available"
        result["package_url"] = remote_info["package_url"]
    else:
        result["action"] = "up_to_date"

    return result
