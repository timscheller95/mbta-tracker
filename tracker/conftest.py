import logging

import pytest

import main

# The OTLP exporters and PeriodicExportingMetricReader start background threads at import
# time but only attempt network I/O when flushing. Suppress the resulting connection-
# refused warnings so they don't pollute test output.
logging.getLogger("opentelemetry.sdk.metrics._internal.export").setLevel(logging.CRITICAL)
logging.getLogger("opentelemetry.exporter.otlp.proto.http").setLevel(logging.CRITICAL)
logging.getLogger("opentelemetry.sdk.trace.export").setLevel(logging.CRITICAL)


@pytest.fixture(autouse=True)
def reset_state():
    """Restore shared tracking state to empty before each test."""
    main._state.vehicle_count_by_direction = {}
    main._state.vehicle_count_by_status = {}
    main._state.vehicle_count_by_occupancy = {}
    main._state.alert_count = 0
    main._state.vehicle_positions = []
    main._state.alert_count_by_effect = {}
    main._state.affected_stops = {}
    main._state.headway_seconds = {}
    main._state.active_alerts = []
    yield
