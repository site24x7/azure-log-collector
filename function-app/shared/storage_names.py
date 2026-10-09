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
    if len(suffix) <= 6:
        return f"s247diag{region}{suffix}"[:24]
    if region == re.sub(r"[^a-z0-9]", "", seed_region.lower()):
        return f"s247dr{suffix}"
    # Include the entire region and suffix; neither loses entropy at 24 chars.
    digest = hashlib.sha256(f"{suffix}:{region}".encode()).hexdigest()[:16]
    return f"s247diag{digest}"


def tenant_storage_name(suffix: str) -> str:
    validate_suffix(suffix)
    return f"s247dt{suffix}" if len(suffix) > 6 else f"s247diagtenant{suffix}"[:24]
