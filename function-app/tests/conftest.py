"""Shared test fixtures for the function-app test suite."""

import os
import sys
import pytest

# Ensure function-app root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove env vars that could leak between tests."""
    for var in (
        "AzureWebJobsStorage",
        "SITE24X7_API_KEY",
        "SITE24X7_BASE_URL",
        "SITE24X7_PROXY_URL",
        "S247_GENERAL_LOGTYPE",
        "UPDATE_CHECK_URL",
        "RESOURCE_GROUP_NAME",
        "RESOURCE_GROUP",
        "WEBSITE_SITE_NAME",
        "SUBSCRIPTION_IDS",
        "FUNCTION_APP_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
