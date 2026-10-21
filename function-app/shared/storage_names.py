"""Stable storage names; preserve legacy deployments and avoid suffix truncation."""
import hashlib
import re


def validate_suffix(suffix: str) -> str:
    if not re.fullmatch(r"[a-z0-9]{1,13}", suffix):
        raise ValueError("Deployment suffix must contain 1–13 lowercase letters or digits")
    return suffix


def regional_storage_name(region: str, suffix: str, seed_region: str = "") -> str:
    region = re.sub(r"[^a-z0-9]", "", region.lower())
    validate_suffix(suffix)
    # A short suffix keeps the readable form only when it fits: cutting at 24
    # characters dropped the suffix in long regions (switzerlandnorth + alpha
    # and + beta both gave s247diagswitzerlandnorth).
    if len(suffix) <= 6 and len(f"s247diag{region}{suffix}") <= 24:
        return f"s247diag{region}{suffix}"
    if region == re.sub(r"[^a-z0-9]", "", seed_region.lower()):
        return f"s247dr{suffix}"
    # Include the entire region and suffix; neither loses entropy at 24 chars.
    digest = hashlib.sha256(f"{suffix}:{region}".encode()).hexdigest()[:16]
    return f"s247diag{digest}"


def tenant_storage_name(suffix: str) -> str:
    validate_suffix(suffix)
    return f"s247dt{suffix}" if len(suffix) > 6 else f"s247diagtenant{suffix}"[:24]


def legacy_regional_storage_name(region: str, suffix: str, seed_region: str = "") -> str:
    """The name earlier versions generated (cut at 24 characters). Still
    recognised as this collector's, so existing accounts keep working."""
    region = re.sub(r"[^a-z0-9]", "", region.lower())
    validate_suffix(suffix)
    if len(suffix) <= 6:
        return f"s247diag{region}{suffix}"[:24]
    return regional_storage_name(region, suffix, seed_region)
