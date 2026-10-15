"""Strip credentials from anything written to, or downloaded from, the debug logs.

Customers send these logs to Site24x7 support, so every event, audit entry
and debug bundle passes through ``redact`` — on write, and again on download
to cover entries stored by older versions.
"""

import re
from typing import Any

MASK = "***"

# Dict keys whose values are always secret, whatever they contain.
_SECRET_KEY = re.compile(
    r"(key|secret|token|password|passwd|credential|signature|sas|"
    r"connection_?string|conn_?str|authorization|cookie)$|^(code|sig)$",
    re.IGNORECASE,
)
# Keys that end in "key" but are identifiers, not secrets.
_SAFE_KEY = re.compile(r"^(cp_key|config_key|blob_key|partition_?key|row_?key|sort_?key|"
                       r"configured_keys|logtype_key|category_key|cache_key)$", re.IGNORECASE)

# Placeholders that only say whether a secret is configured.
_PRESENCE_MARKERS = ("***set***", "NOT SET", MASK)

_PATTERNS = [
    # Old debug messages can contain serialized JSON rather than dictionaries.
    (re.compile(r'(?i)("(?:devicekey|functionkey|accountkey|sharedaccesskey|access_?token|client_?secret|password|apikey|api_key)"\s*:\s*")((?:\\.|[^"\\])*)(")'),
     r"\1" + MASK + r"\3"),
    # key=value pairs in URLs, query strings and connection strings
    (re.compile(r"(?i)\b(devicekey|functionkey|accountkey|sharedaccesskey|sharedaccesssignature|"
                r"access_?token|client_?secret|password|apikey|api_key)=([^&;\s\"'<>]+)"),
     r"\1=" + MASK),
    # short query names are only secrets inside a URL query (keep "error code=500")
    (re.compile(r"(?i)([?&](?:code|sig|skoid|sktid|sks|skv)=)([^&;\s\"'<>]+)"), r"\1" + MASK),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    (re.compile(r"(?i)(x-functions-key|authorization|ocp-apim-subscription-key)"
                r"([\"']?\s*[:=]\s*[\"']?)[^\"',\s}]+"), r"\1\2" + MASK),
]


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any) -> Any:
    """Return a copy of ``value`` with secret fields and embedded credentials masked."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            name = str(k)
            if (_SECRET_KEY.search(name) and not _SAFE_KEY.match(name)
                    and v not in (None, "", False) and v not in _PRESENCE_MARKERS):
                out[k] = MASK
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
