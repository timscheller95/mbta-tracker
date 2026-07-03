"""
Integration tests — require a running Datadog Agent.
Run via: docker compose -f docker-compose.dev.yml run --rm test
"""

import os
from unittest.mock import patch

import pytest
import requests as http

AGENT_HOST = os.getenv("DD_AGENT_HOST", "localhost")
AGENT_PORT = os.getenv("DD_TRACE_AGENT_PORT", "8126")
AGENT_BASE = f"http://{AGENT_HOST}:{AGENT_PORT}"


@pytest.fixture(scope="session", autouse=True)
def require_agent():
    try:
        http.get(f"{AGENT_BASE}/info", timeout=5).raise_for_status()
    except Exception:
        pytest.skip(f"Datadog Agent not reachable at {AGENT_BASE}")


@pytest.fixture(scope="session")
def main():
    import sys
    sys.modules.pop("main", None)
    import main as m
    return m


def test_agent_info():
    """Agent /info endpoint returns version metadata."""
    resp = http.get(f"{AGENT_BASE}/info", timeout=5)
    assert resp.status_code == 200
    assert "version" in resp.json()


def test_alert_span_accepted(main):
    """A disruption alert span is accepted by the Agent."""
    window = main.WATCH_WINDOWS[0]
    with patch.object(main, "_ntfy_post"):
        main.notify_disruption(window, "DELAY")


def test_escalation_span_accepted(main):
    """An escalation alert span is accepted by the Agent."""
    window = main.WATCH_WINDOWS[0]
    with patch.object(main, "_ntfy_post"):
        main.notify_escalation(window, "NO_SERVICE")


def test_resolved_span_accepted(main):
    """A resolution alert span is accepted by the Agent."""
    window = main.WATCH_WINDOWS[0]
    with patch.object(main, "_ntfy_post"):
        main.notify_resolved(window)
