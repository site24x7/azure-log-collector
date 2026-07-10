"""Tests for shared/entra_config.py."""

from shared.entra_config import (
    ENTRA_LOG_CATEGORIES,
    get_entra_categories,
    get_entra_normalized_categories,
)


class TestCategories:
    def test_auditlogs_present(self):
        assert "AuditLogs" in get_entra_categories()
        assert "auditlogs" in get_entra_normalized_categories()

    def test_normalized_matches_blp_derivation(self):
        # BlobLogProcessor normalizes container names via lower() + strip -_ .
        # The normalized value in the config MUST equal that derivation so the
        # S247_<normalized> config key matches the incoming container.
        for entry in ENTRA_LOG_CATEGORIES:
            derived = (
                entry["category"].replace("-", "").replace("_", "").replace(" ", "").lower()
            )
            assert entry["normalized"] == derived, entry["category"]

    def test_counts_align(self):
        assert len(get_entra_categories()) == len(get_entra_normalized_categories())
        assert len(ENTRA_LOG_CATEGORIES) >= 1
