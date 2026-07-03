"""
Integration tests — require network access to MBTA API and optionally healthchecks.io.
Run via: docker compose -f docker-compose.dev.yml run --rm test
"""

import os
import sys
from unittest.mock import patch

import pytest
import requests as http

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(scope="session")
def main():
    sys.modules.pop("main", None)
    import main as m
    return m


def test_mbta_api_reachable():
    """MBTA V3 API responds to a basic Orange Line alerts request."""
    resp = http.get(
        "https://api-v3.mbta.com/alerts",
        params={"filter[route]": "Orange", "page[limit]": "1"},
        headers={"Accept": "application/vnd.api+json"},
        timeout=10,
    )
    assert resp.status_code == 200
    assert "data" in resp.json()


def test_poll_once_runs(main):
    """poll_once() completes without error (ntfy and healthchecks mocked)."""
    with patch.object(main, "_ntfy_post"), patch.object(main, "_ping_healthchecks"):
        main.poll_once()


def test_healthchecks_ping_skipped_when_no_url(main, monkeypatch):
    """_ping_healthchecks() is a no-op when HEALTHCHECKS_URL is not set."""
    monkeypatch.setattr(main, "HEALTHCHECKS_URL", "")
    main._ping_healthchecks()  # should not raise


@pytest.mark.skipif(not os.getenv("HEALTHCHECKS_URL"), reason="HEALTHCHECKS_URL not set")
def test_healthchecks_ping_succeeds(main):
    """Ping URL is reachable and returns 200."""
    resp = http.get(main.HEALTHCHECKS_URL, timeout=5)
    assert resp.status_code == 200
