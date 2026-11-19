"""REST client for the Azure Monitor diagnostic-settings operations.

Replaces the beta azure-mgmt-monitor SDK. The object returned by
``MonitorRestClient`` mimics the four SDK operations azure_manager uses, and
raises the same azure.core exception types so callers need no changes.

ARM api-version 2021-05-01-preview (a preview API, same as the SDK called).
"""

import logging
import os
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

logger = logging.getLogger(__name__)

ARM_ENDPOINT = "https://management.azure.com"
ARM_SCOPE = "https://management.azure.com/.default"
API_VERSION = "2021-05-01-preview"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
MAX_ATTEMPTS = 5
POOL_SIZE = 16  # at least the callers' thread count, so connections are reused
MAX_WAIT = 30.0
_RETRY_STATUSES = (429, 500, 502, 503, 504)

_sdk_warned = False


def _field(obj: Any, *names: str) -> Any:
    """First present field of a dict or object, by any of the given names."""
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) is not None:
                return obj[name]
        elif getattr(obj, name, None) is not None:
            return getattr(obj, name)
    return None


def build_body(parameters: Any) -> Dict[str, Any]:
    """REST JSON body from a dict or SDK model (snake_case or camelCase)."""
    props: Dict[str, Any] = {}
    storage = _field(parameters, "storage_account_id", "storageAccountId")
    if storage:
        props["storageAccountId"] = storage
    logs: List[Dict[str, Any]] = []
    for log in _field(parameters, "logs") or []:
        entry: Dict[str, Any] = {}
        category = _field(log, "category")
        group = _field(log, "category_group", "categoryGroup")
        if category:
            entry["category"] = category
        elif group:
            entry["categoryGroup"] = group
        enabled = _field(log, "enabled")
        entry["enabled"] = bool(enabled) if enabled is not None else False
        logs.append(entry)
    props["logs"] = logs
    metrics = _field(parameters, "metrics")
    props["metrics"] = [m if isinstance(m, dict) else dict(vars(m)) for m in metrics] if metrics else []
    return {"properties": props}


class _ApiError:
    def __init__(self, code: Optional[str], message: Optional[str]):
        self.code = code
        self.message = message


def _error_for(response: "requests.Response", what: str) -> HttpResponseError:
    code = message = None
    try:
        data = response.json()
        err = data.get("error", data) if isinstance(data, dict) else {}
        if isinstance(err, dict):
            code, message = err.get("code"), err.get("message")
    except Exception:
        pass
    text = f"{what} failed: HTTP {response.status_code}" + (f" {code}" if code else "")
    if message:
        text += f": {message}"
    exc = ResourceNotFoundError(text) if response.status_code == 404 else HttpResponseError(text)
    exc.status_code = response.status_code
    exc.error = _ApiError(code, message)
    return exc


def _retry_wait(response: "requests.Response", attempt: int) -> float:
    raw = response.headers.get("Retry-After") if response.headers else None
    try:
        wait = float(raw) if raw is not None else float(2 ** attempt)
    except (TypeError, ValueError):
        wait = float(2 ** attempt)
    return max(0.0, min(wait, MAX_WAIT))


class _Operations:
    def __init__(self, client: "MonitorRestClient"):
        self._c = client

    @staticmethod
    def _base(resource_uri: str) -> str:
        return ARM_ENDPOINT + quote("/" + resource_uri.lstrip("/"), safe="/") + "/providers/Microsoft.Insights/"

    def _setting_url(self, resource_uri: str, name: str) -> str:
        return f"{self._base(resource_uri)}diagnosticSettings/{quote(name, safe='')}?api-version={API_VERSION}"


def _parse_setting(data: Dict[str, Any]) -> SimpleNamespace:
    props = data.get("properties") or {}
    logs = [
        SimpleNamespace(
            category=log.get("category"),
            category_group=log.get("categoryGroup"),
            enabled=bool(log.get("enabled")),
        )
        for log in props.get("logs") or []
    ]
    return SimpleNamespace(
        id=data.get("id"), name=data.get("name"),
        storage_account_id=props.get("storageAccountId"), logs=logs,
    )


class _CategoryOps(_Operations):
    def list(self, resource_uri: str) -> SimpleNamespace:
        url = f"{self._base(resource_uri)}diagnosticSettingsCategories?api-version={API_VERSION}"
        data = self._c.request("GET", url, "list diagnostic categories").json()
        items = [
            SimpleNamespace(
                name=item.get("name"),
                category_type=(item.get("properties") or {}).get("categoryType"),
            )
            for item in data.get("value") or []
        ]
        return SimpleNamespace(value=items)


class _SettingOps(_Operations):
    def get(self, resource_uri: str, name: str) -> SimpleNamespace:
        resp = self._c.request("GET", self._setting_url(resource_uri, name), "get diagnostic setting")
        return _parse_setting(resp.json())

    def create_or_update(self, resource_uri: str, name: str, parameters: Any) -> SimpleNamespace:
        resp = self._c.request("PUT", self._setting_url(resource_uri, name),
                               "create diagnostic setting", body=build_body(parameters))
        return _parse_setting(resp.json())

    def delete(self, resource_uri: str, name: str) -> None:
        # 404 on DELETE: nothing there, so the delete is already done.
        self._c.request("DELETE", self._setting_url(resource_uri, name),
                        "delete diagnostic setting", ok_missing=True)


class MonitorRestClient:
    """Drop-in for the parts of MonitorManagementClient azure_manager uses."""

    def __init__(self, credential: Any):
        self._credential = credential
        self._token: Optional[str] = None
        self._expires = 0.0
        self._lock = threading.Lock()
        # One pooled session: a new TLS connection per call exhausts outbound
        # connections under parallel scans (seen as connect timeouts on Flex Consumption).
        self._session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=1, pool_maxsize=POOL_SIZE)
        self._session.mount("https://", adapter)
        self.diagnostic_settings_category = _CategoryOps(self)
        self.diagnostic_settings = _SettingOps(self)

    def _bearer(self) -> str:
        with self._lock:
            if not self._token or time.time() > self._expires - 300:
                tok = self._credential.get_token(ARM_SCOPE)
                self._token, self._expires = tok.token, float(tok.expires_on)
            return self._token

    def request(self, method: str, url: str, what: str,
                body: Optional[Dict] = None, ok_missing: bool = False) -> "requests.Response":
        for attempt in range(1, MAX_ATTEMPTS + 1):
            headers = {"Authorization": f"Bearer {self._bearer()}"}
            try:
                resp = self._session.request(
                    method, url, headers=headers, json=body,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=False,
                )
            except (requests.ConnectionError, requests.Timeout) as e:
                # Every call here is idempotent (GET, PUT of the same body, DELETE), so a
                # connection that failed or timed out is tried again like a 503.
                if attempt >= MAX_ATTEMPTS:
                    raise
                wait = min(float(2 ** attempt), MAX_WAIT)
                logger.warning("%s: %s, retry %d in %.0fs", what, type(e).__name__, attempt, wait)
                time.sleep(wait)
                continue
            if resp.status_code in _RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                wait = _retry_wait(resp, attempt)
                logger.warning("%s: HTTP %s, retry %d in %.0fs", what, resp.status_code, attempt, wait)
                time.sleep(wait)
                continue
            if 200 <= resp.status_code < 300 or (ok_missing and resp.status_code == 404):
                return resp
            raise _error_for(resp, what)
        raise AssertionError("unreachable")  # loop always returns or raises


def sdk_requested() -> bool:
    return os.environ.get("MONITOR_CLIENT", "").strip().lower() == "sdk"


def warn_sdk_unavailable() -> None:
    global _sdk_warned
    if not _sdk_warned:
        _sdk_warned = True
        logger.warning("MONITOR_CLIENT=sdk but azure-mgmt-monitor is not installed; using REST")
