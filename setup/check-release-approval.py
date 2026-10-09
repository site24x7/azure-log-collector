"""Block stable publication until a release owner confirms live validation."""
import os
import re
from urllib.parse import urlparse


def validate_approval(version, event, confirmed, evidence):
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?", version):
        raise ValueError("Invalid release version")
    if "-" in version:
        return  # Candidates remain publishable for live deployment testing.
    url = urlparse(evidence)
    if (event != "workflow_dispatch" or confirmed != "true"
            or url.scheme != "https" or not url.netloc
            or url.username is not None or url.password is not None):
        raise ValueError(
            "Stable publication blocked: dispatch manually with stable_validation_confirmed=true "
            "and an HTTPS validation_report_url after fresh install, repeat deployment, "
            "upgrade, rollback and real ingestion pass for the matching candidate."
        )


if __name__ == "__main__":
    validate_approval(os.environ.get("RELEASE_VERSION", ""),
                      os.environ.get("RELEASE_EVENT", ""),
                      os.environ.get("STABLE_VALIDATION_CONFIRMED", ""),
                      os.environ.get("VALIDATION_REPORT_URL", ""))
