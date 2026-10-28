"""HTTP functions never call clear_cache(); cached state must still expire."""

import json
from unittest.mock import patch

import shared.config_store as cs

WEBAPP = "/subscriptions/s/resourceGroups/g/providers/Microsoft.Web/sites/webapp"
APIM = "/subscriptions/s/resourceGroups/g/providers/Microsoft.ApiManagement/service/apim"


def test_configured_resources_refresh_after_ttl():
    store = {cs.CONFIGURED_RESOURCES_BLOB: json.dumps({WEBAPP: {"categories": ["A"]}})}
    clock = [1000.0]
    with patch.object(cs, "_read_blob", side_effect=lambda name: store.get(name)), \
         patch.object(cs.time, "monotonic", side_effect=lambda: clock[0]):
        cs.clear_cache()
        assert list(cs.get_configured_resources()) == [WEBAPP]
        # A scan in another process adds APIM to the blob.
        store[cs.CONFIGURED_RESOURCES_BLOB] = json.dumps({WEBAPP: {}, APIM: {}})
        clock[0] += cs.CACHE_TTL_SECONDS / 2
        assert list(cs.get_configured_resources()) == [WEBAPP]          # still cached
        clock[0] += cs.CACHE_TTL_SECONDS
        assert sorted(cs.get_configured_resources()) == sorted([WEBAPP, APIM])  # refreshed


def test_clear_cache_restarts_the_ttl():
    clock = [500.0]
    with patch.object(cs.time, "monotonic", side_effect=lambda: clock[0]):
        cs.clear_cache()
        cs._cache["configured_resources"] = {"x": {}}
        clock[0] += cs.CACHE_TTL_SECONDS - 1
        cs._expire_cache()
        assert cs._cache["configured_resources"] == {"x": {}}
        clock[0] += 2
        cs._expire_cache()
        assert cs._cache["configured_resources"] is None
