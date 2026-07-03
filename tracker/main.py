"""
MBTA Station Slowdown Monitor
=============================
Polls the MBTA V3 API every 60 s and emits three metrics to Datadog via OTLP:

  mbta.station.alert_count         active service alerts (by station + effect)
  mbta.station.next_train_minutes  minutes until next predicted train arrival
  mbta.station.prediction_delay_s  seconds late vs. scheduled arrival

Only Massachusetts Ave and State stations are tracked (Orange Line).
Designed for Raspberry Pi Zero WH: one container, minimal resource use.
"""

import logging
import os
import signal
import time
from datetime import datetime, timezone

import requests
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource

# ── Configuration ─────────────────────────────────────────────────────────────

DD_API_KEY = os.environ["DD_API_KEY"]
DD_SITE = os.getenv("DD_SITE", "datadoghq.com")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Direct OTLP ingestion endpoint — override via OTEL_EXPORTER_OTLP_ENDPOINT if needed.
# For US1 (datadoghq.com) this resolves to https://api.datadoghq.com/api/intake/otlp/v1/metrics
OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    f"https://api.{DD_SITE}/api/intake/otlp",
)

TARGET_STOPS = {
    "place-masta": "Massachusetts Ave",
    "place-state": "State",
}

DIRECTION_LABELS = {0: "southbound", 1: "northbound"}
ACTIVE_LIFECYCLES = {"NEW", "ONGOING", "ONGOING_UPCOMING"}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("mbta.monitor")

# ── OTel Setup ────────────────────────────────────────────────────────────────

_resource = Resource.create({SERVICE_NAME: "mbta-station-monitor"})
_exporter = OTLPMetricExporter(
    endpoint=OTLP_ENDPOINT,
    headers={"DD-API-KEY": DD_API_KEY},
)
_reader = PeriodicExportingMetricReader(
    _exporter,
    export_interval_millis=POLL_INTERVAL * 1000,
)
_meter_provider = MeterProvider(resource=_resource, metric_readers=[_reader])
metrics.set_meter_provider(_meter_provider)
meter = metrics.get_meter(__name__)

# ── Shared State ──────────────────────────────────────────────────────────────


class _State:
    def __init__(self) -> None:
        self.alert_counts: dict[tuple[str, str], int] = {}    # (station, effect) -> count
        self.next_train: dict[tuple[str, str], float] = {}    # (station, direction) -> minutes
        self.delay: dict[tuple[str, str], float] = {}         # (station, direction) -> seconds late


_state = _State()

# ── Observable Gauge Callbacks ────────────────────────────────────────────────


def _observe_alert_count(options):
    for (station, effect), count in _state.alert_counts.items():
        yield Observation(count, {"station": station, "effect": effect, "route": "Orange"})


def _observe_next_train(options):
    for (station, direction), minutes in _state.next_train.items():
        yield Observation(minutes, {"station": station, "direction": direction, "route": "Orange"})


def _observe_delay(options):
    for (station, direction), delay_s in _state.delay.items():
        yield Observation(delay_s, {"station": station, "direction": direction, "route": "Orange"})


# ── Instruments ───────────────────────────────────────────────────────────────

meter.create_observable_gauge(
    "mbta.station.alert_count",
    callbacks=[_observe_alert_count],
    description="Active service alerts at each target station, tagged by effect type",
    unit="{alert}",
)

meter.create_observable_gauge(
    "mbta.station.next_train_minutes",
    callbacks=[_observe_next_train],
    description="Minutes until the next predicted train at each target station",
    unit="min",
)

meter.create_observable_gauge(
    "mbta.station.prediction_delay_s",
    callbacks=[_observe_delay],
    description="How many seconds late the next predicted train is vs. its schedule",
    unit="s",
)

# ── MBTA API ──────────────────────────────────────────────────────────────────

_http = requests.Session()
_http.headers.update({"Accept": "application/vnd.api+json"})


def mbta_get(path: str, params: dict | None = None) -> dict:
    resp = _http.get(f"https://api-v3.mbta.com{path}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Collectors ────────────────────────────────────────────────────────────────


def collect_alerts() -> None:
    data = mbta_get("/alerts", {
        "filter[route]": "Orange",
        "filter[stop]": ",".join(TARGET_STOPS.keys()),
        "filter[activity]": "BOARD,EXIT,RIDE",
    })

    counts: dict[tuple[str, str], int] = {}
    for alert in data.get("data", []):
        attrs = alert.get("attributes", {})
        if attrs.get("lifecycle") not in ACTIVE_LIFECYCLES:
            continue
        effect = attrs.get("effect", "UNKNOWN_EFFECT")
        for entity in attrs.get("informed_entity", []):
            stop_id = entity.get("stop")
            if stop_id in TARGET_STOPS:
                key = (TARGET_STOPS[stop_id], effect)
                counts[key] = counts.get(key, 0) + 1

    _state.alert_counts = counts
    if counts:
        logger.info("alerts: %s", {f"{s}/{e}": c for (s, e), c in counts.items()})
    else:
        logger.info("alerts: none active at target stations")


def collect_predictions() -> None:
    data = mbta_get("/predictions", {
        "filter[route]": "Orange",
        "filter[stop]": ",".join(TARGET_STOPS.keys()),
        "include": "schedule",
        "sort": "arrival_time",
        "page[limit]": "40",
    })

    # Build schedule lookup from the included sidecar objects
    schedules: dict[str, str | None] = {}
    for item in data.get("included", []):
        if item.get("type") == "schedule":
            schedules[item["id"]] = item.get("attributes", {}).get("arrival_time")

    now = datetime.now(timezone.utc)
    next_train: dict[tuple[str, str], float] = {}
    delay: dict[tuple[str, str], float] = {}

    for pred in data.get("data", []):
        attrs = pred.get("attributes", {})
        time_str = attrs.get("arrival_time") or attrs.get("departure_time")
        direction_id = attrs.get("direction_id")
        if time_str is None or direction_id is None:
            continue

        stop_id = (
            pred.get("relationships", {})
            .get("stop", {}).get("data", {}).get("id")
        )
        if stop_id not in TARGET_STOPS:
            continue

        try:
            arrival_dt = datetime.fromisoformat(time_str)
        except (ValueError, TypeError):
            continue

        minutes_away = (arrival_dt - now).total_seconds() / 60
        if minutes_away < 0:
            continue  # already passed

        station = TARGET_STOPS[stop_id]
        direction = DIRECTION_LABELS.get(direction_id, "unknown")
        key = (station, direction)

        if key not in next_train or minutes_away < next_train[key]:
            next_train[key] = minutes_away

            sched_rel = (
                pred.get("relationships", {})
                .get("schedule", {}).get("data")
            )
            if sched_rel:
                sched_arrival = schedules.get(sched_rel.get("id"))
                if sched_arrival:
                    try:
                        sched_dt = datetime.fromisoformat(sched_arrival)
                        delay[key] = (arrival_dt - sched_dt).total_seconds()
                    except (ValueError, TypeError):
                        pass

    _state.next_train = next_train
    _state.delay = delay
    logger.info(
        "predictions: %s | delays: %s",
        {f"{s}/{d}": f"{m:.1f}min" for (s, d), m in next_train.items()},
        {f"{s}/{d}": f"{sec:+.0f}s" for (s, d), sec in delay.items()},
    )


# ── Poll Loop ─────────────────────────────────────────────────────────────────


def poll_once() -> None:
    try:
        collect_alerts()
    except Exception as exc:
        logger.error("alert collection failed: %s", exc)
    try:
        collect_predictions()
    except Exception as exc:
        logger.error("prediction collection failed: %s", exc)


def main() -> None:
    logger.info(
        "MBTA station monitor starting | stops=%s | poll=%ds | otlp=%s",
        list(TARGET_STOPS.values()),
        POLL_INTERVAL,
        OTLP_ENDPOINT,
    )

    running = True

    def _handle_stop(sig, _frame) -> None:
        nonlocal running
        logger.info("shutting down...")
        running = False

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    try:
        while running:
            poll_once()
            # Sleep in 1-second ticks so SIGTERM is handled promptly
            for _ in range(POLL_INTERVAL):
                if not running:
                    break
                time.sleep(1)
    finally:
        logger.info("flushing metrics to Datadog...")
        _meter_provider.shutdown()
        logger.info("stopped")


if __name__ == "__main__":
    main()
