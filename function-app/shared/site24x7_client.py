"""Site24x7 API client with rate limiting, circuit breaker, and log upload.

Provides methods to:
- Query supported Azure log types from Site24x7
- Create Azure log types (returns sourceConfig)
- Parse and upload logs using the proven log_sender.py flow
"""

import os
import re
import sys
import gzip
import json
import time
import hashlib
import logging
import calendar
import datetime
import traceback
import urllib.parse
import urllib.request
from base64 import b64decode
from typing import Optional, Dict, List, Any, Tuple

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Persistent circuit breaker — state survives across function invocations.

    State is saved to blob storage so that if Site24x7 is down, the circuit
    stays open across the 2-minute timer cycles instead of resetting each time.

    States:
        closed    — normal operation, all requests allowed
        open      — S247 unreachable, requests blocked until cooldown expires
        half_open — cooldown expired, one test request allowed

    Cooldown: when circuit opens, it stays open for `recovery_timeout` seconds
    (default 15 min = 900s). This prevents hammering a dead endpoint every 2 min.
    """

    BLOB_NAME = "config/circuit-breaker-state.json"

    def __init__(self, failure_threshold: int = 8, recovery_timeout: int = 300):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = "closed"
        self._load_state()

    def _load_state(self):
        """Load persisted state from blob storage."""
        try:
            conn_str = os.environ.get("AzureWebJobsStorage", "")
            if not conn_str:
                return
            from azure.storage.blob import BlobServiceClient
            blob_service = BlobServiceClient.from_connection_string(conn_str)
            blob_client = blob_service.get_blob_client("s247-config", self.BLOB_NAME)
            stream = blob_client.download_blob()
            self._etag = getattr(stream.properties, "etag", None)
            data = json.loads(stream.readall())
            self.state = data.get("state", "closed")
            self.failure_count = data.get("failure_count", 0)
            self.last_failure_time = data.get("last_failure_time", 0.0)
            if self.state == "open":
                logger.info(
                    "Circuit breaker loaded: OPEN (failures=%d, cooldown remaining=%.0fs)",
                    self.failure_count,
                    max(0, self.recovery_timeout - (time.time() - self.last_failure_time)),
                )
        except Exception:
            self._etag = None  # No persisted state yet — start fresh

    def _save_state(self):
        """Persist state to blob storage with ETag-based concurrency.

        On conflict, re-reads the remote state and merges by taking the
        higher failure_count + most recent last_failure_time, so concurrent
        BlobLogProcessor instances don't clobber each other's failure tallies
        (which could prevent the circuit from ever tripping).
        """
        try:
            conn_str = os.environ.get("AzureWebJobsStorage", "")
            if not conn_str:
                return
            from azure.storage.blob import BlobServiceClient
            from azure.core import MatchConditions
            blob_service = BlobServiceClient.from_connection_string(conn_str)
            container_client = blob_service.get_container_client("s247-config")
            try:
                container_client.create_container()
            except Exception:
                pass
            blob_client = blob_service.get_blob_client("s247-config", self.BLOB_NAME)

            etag = getattr(self, "_etag", None)
            for attempt in range(5):
                payload = json.dumps({
                    "state": self.state,
                    "failure_count": self.failure_count,
                    "last_failure_time": self.last_failure_time,
                    "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                })
                try:
                    if etag:
                        resp = blob_client.upload_blob(
                            payload, overwrite=True,
                            etag=etag,
                            match_condition=MatchConditions.IfNotModified,
                        )
                    else:
                        resp = blob_client.upload_blob(
                            payload, overwrite=True,
                            match_condition=MatchConditions.IfMissing,
                        )
                    self._etag = (resp or {}).get("etag") if isinstance(resp, dict) else None
                    return
                except Exception as e:
                    msg = str(e)
                    if ("ConditionNotMet" in msg or "BlobAlreadyExists" in msg
                            or "412" in msg or "409" in msg):
                        # Merge with remote state before retry.
                        try:
                            stream = blob_client.download_blob()
                            etag = getattr(stream.properties, "etag", None)
                            remote = json.loads(stream.readall())
                            if remote.get("failure_count", 0) > self.failure_count:
                                self.failure_count = remote["failure_count"]
                            if remote.get("last_failure_time", 0) > self.last_failure_time:
                                self.last_failure_time = remote["last_failure_time"]
                            # Remote "open" wins unless we're also open.
                            if remote.get("state") == "open" and self.state == "closed":
                                self.state = "open"
                        except Exception:
                            etag = None
                        time.sleep(0.05 * (attempt + 1))
                        continue
                    return  # non-conflict error — give up silently
        except Exception:
            pass

    def record_success(self):
        prev = self.state
        self.failure_count = 0
        self.state = "closed"
        if prev != "closed":
            logger.info("Circuit breaker CLOSED — S247 recovered")
            self._save_state()

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold and self.state != "open":
            self.state = "open"
            logger.warning(
                "Circuit breaker OPEN after %d failures — "
                "cooldown %ds, no S247 requests until %.0f",
                self.failure_count,
                self.recovery_timeout,
                self.last_failure_time + self.recovery_timeout,
            )
            self._save_state()
            try:
                from shared.debug_logger import log_event
                log_event("error", "CircuitBreaker",
                          f"Circuit OPEN — S247 unreachable after {self.failure_count} failures. "
                          f"Auto-cooldown for {self.recovery_timeout}s.",
                          {"failure_count": self.failure_count,
                           "cooldown_seconds": self.recovery_timeout})
            except Exception:
                pass

    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            elapsed = time.time() - self.last_failure_time
            if elapsed > self.recovery_timeout:
                self.state = "half_open"
                logger.info("Circuit breaker HALF_OPEN — testing S247 connectivity")
                return True
            return False
        return True  # half_open — allow one test request


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, rate: int = 100, per: float = 1.0):
        self.rate = rate
        self.per = per
        self.tokens = float(rate)
        self.last_refill = time.time()

    def acquire(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.rate, self.tokens + elapsed * (self.rate / self.per))
        self.last_refill = now
        if self.tokens < 1:
            sleep_time = (1 - self.tokens) / (self.rate / self.per)
            time.sleep(sleep_time)
            self.tokens = 0
        else:
            self.tokens -= 1


class Site24x7Client:
    """Site24x7 API client for Azure log type management and log upload."""

    def __init__(self):
        self.device_key = os.environ.get("SITE24X7_API_KEY", "")
        self.s247_base_url = os.environ.get(
            "SITE24X7_BASE_URL", "https://www.site24x7.com"
        )
        self.circuit_breaker = CircuitBreaker()
        self.rate_limiter = RateLimiter()

    def _make_s247_request(
        self, path: str, params: Optional[Dict] = None, method: str = "GET"
    ) -> Optional[Dict]:
        """Make an authenticated request to the Site24x7 AppLog servlet."""
        if not self.device_key:
            logger.error("SITE24X7_API_KEY not configured")
            return None

        url_params = {"deviceKey": self.device_key}
        if params:
            url_params.update(params)

        url = f"{self._get_api_base_url()}{path}?{urllib.parse.urlencode(url_params)}"

        try:
            req = urllib.request.Request(url, method=method)
            req.add_header("Accept", "application/json")
            resp = urllib.request.urlopen(req, timeout=120)
            return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error("Site24x7 API request failed (%s): %s", path, e)
            try:
                from shared.debug_logger import log_event
                log_event("error", "Site24x7Client",
                          f"API request failed: {path}",
                          {"url": path, "method": method, "error": str(e),
                           "error_type": type(e).__name__})
            except Exception:
                pass
            return None

    # ─── Azure Log Type Management ───────────────────────────────────────

    # SITE24X7_BASE_URL uses www.* (user-facing), but servlet API calls
    # must go to plus.* (internal AppLog server).
    _API_DOMAIN_MAP = {
        "www.site24x7.com": "plus.site24x7.com",
        "www.site24x7.in": "plus.site24x7.in",
        "www.site24x7.eu": "plus.site24x7.eu",
        "www.site24x7.net.au": "plus.site24x7.net.au",
        "www.site24x7.cn": "plus.site24x7.cn",
        "www.site24x7.jp": "plus.site24x7.jp",
    }

    # Upload domain mapping per Site24x7 data center.
    # SITE24X7_BASE_URL → upload subdomain used by _send_logs_to_s247().
    _UPLOAD_DOMAIN_MAP = {
        "site24x7.com": "logc.site24x7.com",
        "site24x7.in": "logc.site24x7.in",
        "site24x7.eu": "logc.site24x7.eu",
        "site24x7.net.au": "logc.site24x7.net.au",
        "site24x7.cn": "logc.site24x7.cn",
        "site24x7.jp": "logc.site24x7.jp",
    }

    def _get_upload_domain(self) -> str:
        """Derive the log upload domain from SITE24X7_BASE_URL.

        Override with SITE24X7_UPLOAD_DOMAIN env var for local testing
        (e.g., pointing to a mock server).
        """
        override = os.environ.get("SITE24X7_UPLOAD_DOMAIN", "")
        if override:
            return override
        for base_domain, upload_domain in self._UPLOAD_DOMAIN_MAP.items():
            if base_domain in self.s247_base_url:
                return upload_domain
        return "logc.site24x7.com"

    def _get_api_base_url(self) -> str:
        """Derive the AppLog API base URL from SITE24X7_BASE_URL.

        Maps www.site24x7.* → plus.site24x7.* for servlet API calls.
        Falls back to SITE24X7_BASE_URL if no mapping found (e.g., local testing).
        """
        for www_domain, plus_domain in self._API_DOMAIN_MAP.items():
            if www_domain in self.s247_base_url:
                return self.s247_base_url.replace(www_domain, plus_domain)
        return self.s247_base_url

    def get_supported_log_types(self) -> Optional[Dict]:
        """Fetch supported Azure log types from Site24x7.

        Calls GET /applog/azure/logtype_supported?deviceKey=...

        Returns dict with 'supported_types' array containing
        {logtype, display_name, log_categories?} objects.
        """
        result = self._make_s247_request("/applog/azure/logtype_supported")
        if result and str(result.get("status", "")).upper() == "SUCCESS":
            return result
        logger.error("Failed to get supported log types: %s", result)
        return None

    def create_log_type(
        self, category: str, fallback_names: Optional[List[str]] = None,
    ) -> Optional[Dict]:
        """Create/check a single Azure log type via /applog/logtype.

        Calls GET /applog/logtype?deviceKey=...&logType={category}
        The server auto-creates the log type if it's a recognized Azure type.

        If the primary name fails and fallback_names are provided, tries each
        fallback in order (e.g., display_name variants).  This handles the case
        where the server knows the type under a different name.

        Returns a sourceConfig-compatible dict (with apiKey, uploadDomain,
        logType, jsonPath, dateFormat, dateField, filterConfig) ready for
        use by post_logs(), or None on failure.
        """
        import time

        try:
            from shared.debug_logger import log_event as _dbg
        except Exception:
            _dbg = lambda *a, **kw: None

        # Build the list of names to try: primary first, then fallbacks
        names_to_try = [category.replace("-", "").replace("_", "").replace(" ", "").lower()]
        if fallback_names:
            for fb in fallback_names:
                normalized_fb = fb.replace("-", "").replace("_", "").replace(" ", "").lower()
                if normalized_fb not in names_to_try:
                    names_to_try.append(normalized_fb)

        def _is_valid_logtype_response(r):
            if not r:
                return False
            if r.get("logType"):
                return True
            if str(r.get("status", "")).upper() == "SUCCESS":
                return True
            return False

        last_result = None
        for attempt_name in names_to_try:
            _dbg("info", "Site24x7Client",
                 f"create_log_type: trying '{attempt_name}' (raw='{category}')")

            t0 = time.time()
            result = self._make_s247_request(
                "/applog/logtype",
                params={"logType": attempt_name},
            )
            elapsed = time.time() - t0
            last_result = result

            _dbg("info", "Site24x7Client",
                 f"create_log_type: '{attempt_name}' took {elapsed:.1f}s, "
                 f"result_keys={list(result.keys()) if result else None}")

            if _is_valid_logtype_response(result):
                if not result.get("apiUpload"):
                    logger.warning("Log type '%s' does not allow API upload", attempt_name)
                    continue
                return self._build_source_config(result, attempt_name)

            # First attempt failed — retry once (server may auto-create on first call)
            _dbg("warning", "Site24x7Client",
                 f"create_log_type: '{attempt_name}' invalid, "
                 f"result={str(result)[:200]}. Retrying...")
            time.sleep(2)

            t1 = time.time()
            result = self._make_s247_request(
                "/applog/logtype",
                params={"logType": attempt_name},
            )
            elapsed2 = time.time() - t1
            last_result = result

            _dbg("info", "Site24x7Client",
                 f"create_log_type: '{attempt_name}' retry took {elapsed2:.1f}s, "
                 f"result_keys={list(result.keys()) if result else None}")

            if _is_valid_logtype_response(result):
                if not result.get("apiUpload"):
                    logger.warning("Log type '%s' does not allow API upload", attempt_name)
                    continue
                return self._build_source_config(result, attempt_name)

            logger.info(
                "Log type '%s' not recognized by server — trying next fallback",
                attempt_name,
            )

        # All names exhausted
        _dbg("error", "Site24x7Client",
             f"create_log_type: ALL attempts FAILED for '{category}' "
             f"(tried {names_to_try}), last_result={str(last_result)[:200]}")
        logger.error(
            "Failed to create/check log type for '%s' (tried %s): %s",
            category, names_to_try, last_result,
        )
        return None

    def _build_source_config(self, result: Dict, normalized: str) -> Dict:
        """Build a sourceConfig dict from a /applog/logtype response."""
        source_config = {
            "apiKey": self.device_key,
            "logType": result.get("logType", normalized),
            "uploadDomain": self._get_upload_domain(),
            "dateField": result.get("dateField", "time"),
            "dateFormat": result.get("dateFormat", "%Y-%m-%dT%H:%M:%S.%f"),
        }

        if "json_path" in result:
            source_config["jsonPath"] = result["json_path"]
        elif "jsonPath" in result:
            source_config["jsonPath"] = result["jsonPath"]

        if "filterConfig" in result:
            source_config["filterConfig"] = result["filterConfig"]
        if "masking" in result:
            source_config["maskingConfig"] = result["masking"]
        elif "maskingConfig" in result:
            source_config["maskingConfig"] = result["maskingConfig"]
        if "hashing" in result:
            source_config["hashingConfig"] = result["hashing"]
        elif "hashingConfig" in result:
            source_config["hashingConfig"] = result["hashingConfig"]
        if "derived" in result:
            source_config["derivedConfig"] = result["derived"]
        elif "derivedConfig" in result:
            source_config["derivedConfig"] = result["derivedConfig"]

        return source_config

    def create_log_types(
        self,
        categories: List[str],
        supported_types: Optional[Dict] = None,
    ) -> Optional[List[Dict]]:
        """Create/check multiple Azure log types via /applog/logtype.

        Uses parallel threads for speed (relay adds ~7s latency per call).
        When supported_types is provided, builds fallback names from the
        display_name so the server can match even if the primary name isn't
        recognized (e.g., 'joblogs' → fallback 'automationrunbookjobs').

        Returns list of dicts with 'category' (S247_{name}) and 'sourceConfig',
        plus 'errors' list with details of any failures.
        """
        if not categories:
            return []

        from concurrent.futures import ThreadPoolExecutor, as_completed

        results = []
        errors = []

        def _fallback_names(category):
            """Build fallback name list from supported_types."""
            if not supported_types:
                return None
            normalized = category.replace("-", "").replace("_", "").replace(" ", "").lower()
            info = supported_types.get(normalized, {})
            display = info.get("display_name", "")
            if display:
                return [display]
            return None

        def _create_one(category):
            config = self.create_log_type(category, fallback_names=_fallback_names(category))
            if config:
                return {"category": f"S247_{category}", "sourceConfig": config, "error": None}
            error_detail = {
                "category": category,
                "message": f"Server did not recognize log type '{category}'",
            }
            logger.warning("Skipping category '%s' — no config returned", category)
            return {"category": None, "sourceConfig": None, "error": error_detail}

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(_create_one, cat): cat for cat in categories}
            for future in as_completed(futures):
                category = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    # Don't let one category's failure abort the whole batch
                    logger.error(
                        "create_log_types: uncaught exception for '%s': %s",
                        category, e,
                    )
                    errors.append({
                        "category": category,
                        "message": f"Unexpected error: {e}",
                    })
                    continue
                if result and result.get("sourceConfig"):
                    results.append(result)
                elif result and result.get("error"):
                    errors.append(result["error"])

        # Attach errors to the result for the caller to inspect.
        # If every create failed, still surface the errors via a sentinel
        # entry so the caller (DiagSettingsManager) can log them and
        # continue the scan without thinking the server is unreachable.
        if results:
            results[0]["_errors"] = errors
            return results
        if errors:
            return [{"category": None, "sourceConfig": None, "_errors": errors}]
        return None

    def preflight_check(self) -> Dict:
        """Quick connectivity check — call before a scan to validate the
        full relay/API chain. Uses the supported-types endpoint because it
        only validates the device key + relay reachability and never returns
        "Log Type Not Found" for a probe value that may not exist on the
        server. Returns dict with 'ok' bool, 'latency_ms', and 'error'."""
        t0 = time.time()
        try:
            result = self._make_s247_request("/applog/azure/logtype_supported")
            latency = int((time.time() - t0) * 1000)
            if result and str(result.get("status", "")).upper() == "SUCCESS":
                return {"ok": True, "latency_ms": latency}
            return {
                "ok": False,
                "latency_ms": latency,
                "error": f"Unexpected response: {str(result)[:200]}",
            }
        except Exception as e:
            return {
                "ok": False,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": f"{type(e).__name__}: {e}",
            }

    # ─── Log Upload (from log_sender.py) ─────────────────────────────────

    def post_logs(self, source_config_b64: str, log_events: List[Dict]) -> bool:
        """Parse and POST logs to Site24x7 using the log_sender.py flow.

        Args:
            source_config_b64: Base64-encoded sourceConfig JSON
            log_events: Raw log event records from Azure diagnostic logs
        """
        if not self.circuit_breaker.can_execute():
            logger.warning("Circuit breaker OPEN — skipping log POST")
            return False

        self.rate_limiter.acquire()

        try:
            config = json.loads(b64decode(source_config_b64).decode("utf-8"))

            # Always use the live upload domain and API key (blob configs may
            # have stale values from an earlier scan or different environment)
            config["uploadDomain"] = self._get_upload_domain()
            config["apiKey"] = self.device_key

            # Prepare masking/hashing/derived configs
            masking_config = config.get("maskingConfig")
            hashing_config = config.get("hashingConfig")
            derived_eval = config.get("derivedConfig")
            derived_fields = None

            if derived_eval:
                derived_fields = {}
                for key in derived_eval:
                    derived_fields[key] = []
                    for values in derived_eval[key]:
                        derived_fields[key].append(
                            re.compile(values.replace("\\\\", "\\").replace("?<", "?P<"))
                        )

            if masking_config:
                for key in masking_config:
                    masking_config[key]["regex"] = re.compile(masking_config[key]["regex"])

            if hashing_config:
                for key in hashing_config:
                    hashing_config[key]["regex"] = re.compile(hashing_config[key]["regex"])

            if "filterConfig" in config:
                for field in config["filterConfig"]:
                    temp = []
                    for value in config["filterConfig"][field]["values"]:
                        temp.append(re.compile(value))
                    config["filterConfig"][field]["values"] = "|".join(
                        x.pattern for x in temp
                    )

            # Parse logs
            parsed_lines, log_size = _json_log_parser(
                log_events, config, masking_config, hashing_config, derived_fields
            )

            if not parsed_lines:
                logger.info("No parsed lines to upload after filtering")
                self.circuit_breaker.record_success()
                return True

            # Compress and upload
            gzipped = gzip.compress(json.dumps(parsed_lines).encode())
            _send_logs_to_s247(config, gzipped, log_size)

            self.circuit_breaker.record_success()
            logger.info("Uploaded %d log records to Site24x7", len(parsed_lines))
            return True

        except Exception as e:
            self.circuit_breaker.record_failure()
            logger.error("Failed to post logs to Site24x7: %s", e)
            traceback.print_exc()
            try:
                from shared.debug_logger import log_event
                log_event("error", "Site24x7Client",
                          f"Log upload failed: {e}",
                          {"error": str(e), "error_type": type(e).__name__,
                           "record_count": len(log_events),
                           "circuit_breaker_state": self.circuit_breaker.state})
            except Exception:
                pass
            return False

    def get_general_log_type_config(self) -> Optional[str]:
        """Get the general catch-all log type config (base64)."""
        return os.environ.get("S247_GENERAL_LOGTYPE")


# ─── Log Parsing Functions (extracted from log_sender.py) ────────────────


def _get_timestamp(datetime_string: str, format_string: str) -> int:
    try:
        datetime_data = datetime.datetime.strptime(
            datetime_string[:26], format_string
        )
        timestamp = (
            calendar.timegm(datetime_data.utctimetuple()) * 1000
            + int(datetime_data.microsecond / 1000)
        )
        return int(timestamp)
    except Exception:
        return 0


def _get_json_value(obj, key, datatype=None):
    if key in obj or key.lower() in obj:
        if datatype and datatype == "json-object":
            arr_json = []
            child_obj = obj[key]
            if isinstance(child_obj, str):
                try:
                    child_obj = json.loads(child_obj, strict=False)
                except Exception:
                    child_obj = json.loads(
                        child_obj.replace("\\", "\\\\"), strict=False
                    )
            for child_key in child_obj:
                arr_json.append({"key": child_key, "value": str(child_obj[child_key])})
            return arr_json
        else:
            return obj[key] if key in obj else obj[key.lower()]
    elif "." in key:
        parent_key = key[: key.index(".")]
        child_key = key[key.index(".") + 1 :]
        parent_val = parent_key if parent_key in obj else parent_key.capitalize()
        if parent_val not in obj:
            return None
        child_obj = obj[parent_val]
        if isinstance(child_obj, str):
            try:
                child_obj = json.loads(child_obj, strict=False)
            except Exception:
                child_obj = json.loads(
                    child_obj.replace("\\", "\\\\"), strict=False
                )
        return _get_json_value(child_obj, child_key)
    return None


def _is_filters_matched(formatted_line: Dict, config: Dict) -> bool:
    if "filterConfig" in config:
        for field in config["filterConfig"]:
            if field in formatted_line:
                if re.findall(
                    config["filterConfig"][field]["values"],
                    str(formatted_line[field]),
                ):
                    val = True
                else:
                    val = False
                if config["filterConfig"][field]["match"] ^ val:
                    return False
    return True


def _apply_masking(formatted_line: Dict, masking_config: Dict) -> int:
    adjust_total = 0
    try:
        for config_key in masking_config:
            adjust_length = 0
            mask_regex = masking_config[config_key]["regex"]
            if config_key in formatted_line:
                field_value = str(formatted_line[config_key])
                for matcher in re.finditer(mask_regex, field_value):
                    for i in range(mask_regex.groups):
                        matched_value = matcher.group(i + 1)
                        if matched_value:
                            start = matcher.start(i + 1) - adjust_length
                            end = matcher.end(i + 1) - adjust_length
                            if start >= 0 and end > 0:
                                adjust_length += (end - start) - len(
                                    masking_config[config_key]["string"]
                                )
                                field_value = (
                                    field_value[:start]
                                    + masking_config[config_key]["string"]
                                    + field_value[end:]
                                )
                formatted_line[config_key] = field_value
                adjust_total += adjust_length
    except Exception:
        traceback.print_exc()
    return adjust_total


def _apply_hashing(formatted_line: Dict, hashing_config: Dict) -> int:
    adjust_total = 0
    try:
        for config_key in hashing_config:
            adjust_length = 0
            mask_regex = hashing_config[config_key]["regex"]
            if config_key in formatted_line:
                field_value = str(formatted_line[config_key])
                for matcher in re.finditer(mask_regex, field_value):
                    for i in range(mask_regex.groups):
                        matched_value = matcher.group(i + 1)
                        if matched_value:
                            start = matcher.start(i + 1) - adjust_length
                            end = matcher.end(i + 1) - adjust_length
                            if start >= 0 and end > 0:
                                hash_string = hashlib.sha256(
                                    matched_value.encode("utf-8")
                                ).hexdigest()
                                adjust_length += (end - start) - len(hash_string)
                                field_value = (
                                    field_value[:start]
                                    + hash_string
                                    + field_value[end:]
                                )
                formatted_line[config_key] = field_value
                adjust_total += adjust_length
    except Exception:
        traceback.print_exc()
    return adjust_total


def _apply_derived_fields(
    formatted_line: Dict, derived_fields: Dict
) -> int:
    added_size = 0
    try:
        for items in derived_fields:
            for each in derived_fields[items]:
                if items in formatted_line:
                    match_derived = each.search(str(formatted_line[items]))
                    if match_derived:
                        match_derived_field = match_derived.groupdict(default="-")
                        formatted_line.update(match_derived_field)
                        for field_name in match_derived_field:
                            added_size += len(str(formatted_line[field_name]))
                        break
    except Exception:
        traceback.print_exc()
    return added_size


def _json_log_parser(
    log_events: List[Dict],
    config: Dict,
    masking_config: Optional[Dict],
    hashing_config: Optional[Dict],
    derived_fields: Optional[Dict],
) -> Tuple[List[Dict], int]:
    """Parse raw Azure diagnostic log events using jsonPath config."""
    log_size = 0
    parsed_lines = []
    date_format = config.get("dateFormat", "%Y-%m-%dT%H:%M:%S.%f")
    date_field = config.get("dateField", "time")

    for event_obj in log_events:
        try:
            formatted_line = {}
            json_log_size = 0

            for path_obj in config.get("jsonPath", []):
                key = path_obj.get("key") or path_obj.get("name", "")
                datatype = path_obj.get("type")
                value = _get_json_value(event_obj, key, datatype)
                if value is not None:
                    formatted_line[path_obj["name"]] = value
                    json_log_size += len(str(value))

            if not _is_filters_matched(formatted_line, config):
                continue

            log_size += json_log_size

            # Add timestamp
            if date_field in event_obj:
                formatted_line["_zl_timestamp"] = _get_timestamp(
                    event_obj[date_field], date_format
                )

            # Extract resource group as agent uid
            if "resourceId" in event_obj:
                parts = event_obj["resourceId"].split("/")
                if len(parts) > 4:
                    formatted_line["s247agentuid"] = parts[4]
                event_obj["resourceId"] = event_obj["resourceId"].lower()

            # Apply transformations
            if masking_config:
                log_size -= _apply_masking(formatted_line, masking_config)
            if hashing_config:
                log_size -= _apply_hashing(formatted_line, hashing_config)
            if derived_fields:
                log_size += _apply_derived_fields(formatted_line, derived_fields)

            parsed_lines.append(formatted_line)

        except Exception as e:
            logger.warning("Unable to parse event: %s — %s", event_obj, e)

    return parsed_lines, log_size


def _send_logs_to_s247(config: Dict, gzipped_data: bytes, log_size: int) -> None:
    """POST gzipped log data to Site24x7 upload endpoint."""
    header_obj = {
        "X-DeviceKey": config["apiKey"],
        "X-LogType": config["logType"],
        "X-StreamMode": "1",
        "Log-Size": str(log_size),
        "Content-Type": "application/json",
        "Content-Encoding": "gzip",
        "User-Agent": "AZURE-DiagLogs-Function",
    }
    upload_domain = config['uploadDomain']
    upload_url = f"https://{upload_domain}/upload"
    request = urllib.request.Request(upload_url, headers=header_obj)
    response = urllib.request.urlopen(request, data=gzipped_data, timeout=120)
    resp_headers = dict(response.getheaders())
    upload_id = resp_headers.get("x-uploadid", "unknown")

    if response.status == 200:
        logger.info("%s: Logs uploaded to Site24x7 successfully", upload_id)
    else:
        logger.error(
            "%s: Upload failed — status=%d, reason=%s",
            upload_id,
            response.status,
            response.read(),
        )
