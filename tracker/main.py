"""
MBTA Orange Line Tracker
========================
Polls the MBTA V3 REST API for Orange Line vehicle positions and service alerts,
then emits OpenTelemetry traces and metrics via OTLP/HTTP to an OTel Collector.

The Collector dual-ships to:
  - Prometheus (scraped by Grafana)
  - Datadog
"""

import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Generator

import requests
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import (
    SERVICE_NAME,
    SERVICE_NAMESPACE,
    SERVICE_VERSION,
    Resource,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode

# ── Configuration ─────────────────────────────────────────────────────────────

MBTA_API_KEY = os.getenv("MBTA_API_KEY", "")
MBTA_BASE_URL = "https://api-v3.mbta.com"
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
DEPLOY_ENV = os.getenv("DEPLOYMENT_ENV", "development")

# Orange Line: direction_id 0 = Forest Hills (southbound), 1 = Oak Grove (northbound)
DIRECTION_LABELS = {0: "southbound", 1: "northbound"}

# Human-readable names for every Orange Line station (north → south)
ORANGE_LINE_STOPS: dict[str, str] = {
    "place-ogmnl": "Oak Grove",
    "place-mlmnl": "Malden Center",
    "place-welln": "Wellington",
    "place-astao": "Assembly",
    "place-sull":  "Sullivan Square",
    "place-ccmnl": "Community College",
    "place-north": "North Station",
    "place-haecl": "Haymarket",
    "place-state": "State",
    "place-dwnxg": "Downtown Crossing",
    "place-chncl": "Chinatown",
    "place-tumnl": "Tufts Medical Center",
    "place-bbsta": "Back Bay",
    "place-masta": "Massachusetts Ave",
    "place-rugg":  "Ruggles",
    "place-rcmnl": "Roxbury Crossing",
    "place-jaksn": "Jackson Square",
    "place-sbmnl": "Stony Brook",
    "place-grnst": "Green Street",
    "place-forhl": "Forest Hills",
}

# Central stop used to measure headway (time between consecutive trains)
HEADWAY_REFERENCE_STOP = "place-dwnxg"  # Downtown Crossing

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("mbta.tracker")

# ── Shared state for observable metric callbacks ───────────────────────────────

class _State:
    def __init__(self) -> None:
        self.vehicle_count_by_direction: dict[str, int] = {}
        self.vehicle_count_by_status: dict[str, int] = {}
        self.vehicle_count_by_occupancy: dict[str, int] = {}
        self.alert_count: int = 0
        # Per-vehicle position data for the map panel
        self.vehicle_positions: list[dict] = []
        # Rider-focused alert breakdowns
        self.alert_count_by_effect: dict[tuple[str, str], int] = {}  # (effect, direction) → count
        self.affected_stops: dict[tuple[str, str, str, str], int] = {}  # (id, name, effect, dir) → 1
        self.headway_seconds: dict[str, float] = {}  # direction → seconds
        # Full detail per active alert (for info metric)
        self.active_alerts: list[dict] = []  # [{alert_id, effect, direction, header}, ...]

_state = _State()

# ── OTel Resource ─────────────────────────────────────────────────────────────

_resource = Resource.create(
    {
        SERVICE_NAME: "mbta-orange-line-tracker",
        SERVICE_VERSION: "1.0.0",
        SERVICE_NAMESPACE: "mbta",
        "deployment.environment": DEPLOY_ENV,
        "mbta.route": "Orange",
    }
)

# ── Trace Provider ────────────────────────────────────────────────────────────

_trace_exporter = OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces")
_tracer_provider = TracerProvider(resource=_resource)
_tracer_provider.add_span_processor(BatchSpanProcessor(_trace_exporter))
trace.set_tracer_provider(_tracer_provider)

tracer = trace.get_tracer(__name__, "1.0.0")

# ── Meter Provider ────────────────────────────────────────────────────────────

_metric_exporter = OTLPMetricExporter(endpoint=f"{OTEL_ENDPOINT}/v1/metrics")
_metric_reader = PeriodicExportingMetricReader(
    _metric_exporter,
    export_interval_millis=POLL_INTERVAL * 1000,
)
_meter_provider = MeterProvider(resource=_resource, metric_readers=[_metric_reader])
metrics.set_meter_provider(_meter_provider)

meter = metrics.get_meter(__name__, "1.0.0")

# ── Observable Gauge Callbacks ────────────────────────────────────────────────


def _observe_vehicle_count_by_direction(
    options,
) -> Generator[Observation, None, None]:
    for direction, count in _state.vehicle_count_by_direction.items():
        yield Observation(count, {"direction": direction, "route": "Orange"})


def _observe_vehicle_count_by_status(
    options,
) -> Generator[Observation, None, None]:
    for status, count in _state.vehicle_count_by_status.items():
        yield Observation(count, {"status": status, "route": "Orange"})


def _observe_vehicle_count_by_occupancy(
    options,
) -> Generator[Observation, None, None]:
    for occupancy, count in _state.vehicle_count_by_occupancy.items():
        yield Observation(count, {"occupancy_status": occupancy, "route": "Orange"})


def _observe_alert_count(options) -> Generator[Observation, None, None]:
    yield Observation(_state.alert_count, {"route": "Orange"})


def _observe_vehicle_latitude(options) -> Generator[Observation, None, None]:
    for v in _state.vehicle_positions:
        yield Observation(
            v["lat"],
            {
                "vehicle_id": v["vehicle_id"],
                "label": v["label"],
                "direction": v["direction"],
                "status": v["status"],
                "route": "Orange",
            },
        )


def _observe_vehicle_longitude(options) -> Generator[Observation, None, None]:
    for v in _state.vehicle_positions:
        yield Observation(
            v["lng"],
            {
                "vehicle_id": v["vehicle_id"],
                "label": v["label"],
                "direction": v["direction"],
                "status": v["status"],
                "route": "Orange",
            },
        )


def _observe_alert_by_effect(options) -> Generator[Observation, None, None]:
    for (effect, direction), count in _state.alert_count_by_effect.items():
        yield Observation(count, {"effect": effect, "direction": direction, "route": "Orange"})


def _observe_stop_alert(options) -> Generator[Observation, None, None]:
    for (stop_id, stop_name, effect, direction), val in _state.affected_stops.items():
        yield Observation(
            val,
            {
                "stop_id":   stop_id,
                "stop_name": stop_name,
                "effect":    effect,
                "direction": direction,
                "route":     "Orange",
            },
        )


def _observe_headway(options) -> Generator[Observation, None, None]:
    for direction, seconds in _state.headway_seconds.items():
        yield Observation(
            seconds,
            {"direction": direction, "route": "Orange", "reference_stop": "Downtown Crossing"},
        )


def _observe_alert_info(options) -> Generator[Observation, None, None]:
    for alert in _state.active_alerts:
        yield Observation(
            1,
            {
                "alert_id":  alert["alert_id"],
                "effect":    alert["effect"],
                "direction": alert["direction"],
                "header":    alert["header"],
                "route":     "Orange",
            },
        )


# ── Metric Instruments ────────────────────────────────────────────────────────

# Gauges via observable callbacks (current state snapshots)
meter.create_observable_gauge(
    name="mbta.vehicle.count",
    callbacks=[_observe_vehicle_count_by_direction],
    description="Number of active Orange Line vehicles by direction of travel",
    unit="{vehicle}",
)

meter.create_observable_gauge(
    name="mbta.vehicle.status",
    callbacks=[_observe_vehicle_count_by_status],
    description="Number of Orange Line vehicles by current stop status",
    unit="{vehicle}",
)

meter.create_observable_gauge(
    name="mbta.vehicle.occupancy",
    callbacks=[_observe_vehicle_count_by_occupancy],
    description="Number of Orange Line vehicles by passenger occupancy level",
    unit="{vehicle}",
)

meter.create_observable_gauge(
    name="mbta.alert.count",
    callbacks=[_observe_alert_count],
    description="Number of active service alerts affecting the Orange Line",
    unit="{alert}",
)

meter.create_observable_gauge(
    name="mbta.vehicle.latitude",
    callbacks=[_observe_vehicle_latitude],
    description="Current latitude of each active Orange Line vehicle",
    unit="deg",
)

meter.create_observable_gauge(
    name="mbta.vehicle.longitude",
    callbacks=[_observe_vehicle_longitude],
    description="Current longitude of each active Orange Line vehicle",
    unit="deg",
)

meter.create_observable_gauge(
    name="mbta.alert.by_effect",
    callbacks=[_observe_alert_by_effect],
    description="Active Orange Line alerts grouped by service effect and direction",
    unit="{alert}",
)

meter.create_observable_gauge(
    name="mbta.stop.alert",
    callbacks=[_observe_stop_alert],
    description="Alert indicator per Orange Line stop (1 = currently affected by an alert)",
    unit="{alert}",
)

meter.create_observable_gauge(
    name="mbta.headway",
    callbacks=[_observe_headway],
    description="Average time between consecutive Orange Line trains at Downtown Crossing",
    unit="s",
)

meter.create_observable_gauge(
    name="mbta.alert.info",
    callbacks=[_observe_alert_info],
    description="Active Orange Line alerts with human-readable header text (info metric, always 1)",
    unit="{alert}",
)

# Direct instruments for API telemetry
_api_duration = meter.create_histogram(
    name="mbta.api.request.duration",
    description="End-to-end latency of MBTA V3 API requests",
    unit="s",
)

_api_requests = meter.create_counter(
    name="mbta.api.requests",
    description="Total number of MBTA V3 API requests made",
    unit="{request}",
)

_api_errors = meter.create_counter(
    name="mbta.api.errors",
    description="Total number of failed MBTA V3 API requests",
    unit="{error}",
)

# Histogram for vehicle speed distribution
_vehicle_speed = meter.create_histogram(
    name="mbta.vehicle.speed",
    description="Distribution of Orange Line vehicle speeds",
    unit="mph",
)

# ── MBTA API Client ───────────────────────────────────────────────────────────

_http = requests.Session()
_http.headers.update({"Accept": "application/vnd.api+json"})
if MBTA_API_KEY:
    _http.headers["x-api-key"] = MBTA_API_KEY


def mbta_get(path: str, params: dict | None = None) -> dict:
    """
    Execute a GET request against the MBTA V3 API.

    Wraps each call in an OTel client span and records request duration,
    request count, and error count as metrics.
    """
    url = f"{MBTA_BASE_URL}{path}"
    endpoint = path.strip("/").split("?")[0]

    with tracer.start_as_current_span(
        f"GET /api/{endpoint}",
        kind=SpanKind.CLIENT,
    ) as span:
        span.set_attribute("http.method", "GET")
        span.set_attribute("http.url", url)
        span.set_attribute("server.address", "api-v3.mbta.com")
        span.set_attribute("server.port", 443)
        span.set_attribute("mbta.endpoint", endpoint)
        if params:
            for k, v in params.items():
                span.set_attribute(f"mbta.param.{k}", str(v))

        t0 = time.monotonic()
        try:
            resp = _http.get(url, params=params, timeout=10)
            elapsed = time.monotonic() - t0

            span.set_attribute("http.response.status_code", resp.status_code)
            status = str(resp.status_code)

            _api_duration.record(elapsed, {"endpoint": endpoint, "http.status_code": status})
            _api_requests.add(1, {"endpoint": endpoint, "http.status_code": status})

            resp.raise_for_status()

            body = resp.json()
            result_count = len(body.get("data", []))
            span.set_attribute("mbta.result_count", result_count)
            return body

        except requests.HTTPError as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            _api_errors.add(1, {"endpoint": endpoint, "error": "http_error"})
            raise

        except requests.RequestException as exc:
            elapsed = time.monotonic() - t0
            _api_duration.record(elapsed, {"endpoint": endpoint, "http.status_code": "error"})
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            _api_errors.add(1, {"endpoint": endpoint, "error": type(exc).__name__})
            raise


# ── Data Collection ───────────────────────────────────────────────────────────


def collect_vehicles() -> int:
    """
    Fetch Orange Line vehicle positions from the MBTA API.

    Updates shared state for direction, status, and occupancy gauge callbacks,
    and records individual vehicle speeds into a histogram.

    Returns the total number of active vehicles found.
    """
    with tracer.start_as_current_span("mbta.collect.vehicles") as span:
        payload = mbta_get("/vehicles", {"filter[route]": "Orange"})
        vehicles = payload.get("data", [])

        by_direction: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_occupancy: dict[str, int] = {}
        positions: list[dict] = []

        for vehicle in vehicles:
            attrs = vehicle.get("attributes", {})
            direction = DIRECTION_LABELS.get(attrs.get("direction_id", -1), "unknown")
            status = attrs.get("current_status", "UNKNOWN")
            occupancy = attrs.get("occupancy_status") or "NO_DATA"
            speed_mps: float | None = attrs.get("speed")
            lat: float | None = attrs.get("latitude")
            lng: float | None = attrs.get("longitude")

            by_direction[direction] = by_direction.get(direction, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
            by_occupancy[occupancy] = by_occupancy.get(occupancy, 0) + 1

            if lat is not None and lng is not None:
                positions.append({
                    "vehicle_id": vehicle.get("id", "unknown"),
                    "label": attrs.get("label", "?"),
                    "direction": direction,
                    "status": status,
                    "lat": lat,
                    "lng": lng,
                })

            if speed_mps is not None:
                speed_mph = speed_mps * 2.23694
                _vehicle_speed.record(
                    speed_mph,
                    {"direction": direction, "route": "Orange"},
                )

        _state.vehicle_count_by_direction = by_direction
        _state.vehicle_count_by_status = by_status
        _state.vehicle_count_by_occupancy = by_occupancy
        _state.vehicle_positions = positions

        total = len(vehicles)
        span.set_attribute("mbta.vehicle.total", total)
        span.set_attribute("mbta.vehicle.northbound", by_direction.get("northbound", 0))
        span.set_attribute("mbta.vehicle.southbound", by_direction.get("southbound", 0))
        span.set_attribute("mbta.vehicle.status_breakdown", str(by_status))

        logger.info(
            "vehicles collected | total=%d northbound=%d southbound=%d status=%s",
            total,
            by_direction.get("northbound", 0),
            by_direction.get("southbound", 0),
            by_status,
        )
        return total


def collect_alerts() -> int:
    """
    Fetch active Orange Line service alerts and extract rider-relevant detail.

    Parses effect type (DELAY, STOP_CLOSURE, SUSPENSION, etc.), affected direction,
    and the specific Orange Line stops named in each alert's informed_entity list.

    Returns the count of active alerts.
    """
    with tracer.start_as_current_span("mbta.collect.alerts") as span:
        payload = mbta_get(
            "/alerts",
            {
                "filter[route]": "Orange",
                "filter[activity]": "BOARD,EXIT,RIDE",
            },
        )
        alerts = payload.get("data", [])

        active_lifecycles = {"NEW", "ONGOING", "ONGOING_UPCOMING"}
        active = [a for a in alerts if a.get("attributes", {}).get("lifecycle") in active_lifecycles]

        effect_counts: dict[tuple[str, str], int] = {}
        affected_stops: dict[tuple[str, str, str, str], int] = {}
        active_alerts: list[dict] = []

        for alert in active:
            attrs = alert.get("attributes", {})
            effect = attrs.get("effect", "UNKNOWN_EFFECT")
            header = (attrs.get("header") or "").strip()
            entities = attrs.get("informed_entity", [])

            # Determine direction: if every entity shares the same direction_id, use it
            direction_ids = {
                e["direction_id"] for e in entities if e.get("direction_id") is not None
            }
            direction = (
                DIRECTION_LABELS.get(direction_ids.pop(), "unknown")
                if len(direction_ids) == 1
                else "both"
            )

            key = (effect, direction)
            effect_counts[key] = effect_counts.get(key, 0) + 1

            active_alerts.append({
                "alert_id":  alert.get("id", ""),
                "effect":    effect,
                "direction": direction,
                "header":    header,
            })

            # Record which known Orange Line stops are named in this alert
            for entity in entities:
                stop_id = entity.get("stop")
                if stop_id and stop_id in ORANGE_LINE_STOPS:
                    stop_key = (stop_id, ORANGE_LINE_STOPS[stop_id], effect, direction)
                    affected_stops[stop_key] = 1

        _state.alert_count = len(active)
        _state.alert_count_by_effect = effect_counts
        _state.affected_stops = affected_stops
        _state.active_alerts = active_alerts

        span.set_attribute("mbta.alert.total", len(alerts))
        span.set_attribute("mbta.alert.active", len(active))
        span.set_attribute("mbta.alert.effects", str(effect_counts))

        logger.info(
            "alerts collected | active=%d effects=%s stops_impacted=%d",
            len(active),
            effect_counts,
            len(affected_stops),
        )
        return len(active)


def collect_predictions() -> None:
    """
    Fetch upcoming Orange Line arrival predictions at Downtown Crossing.

    Computes average headway (gap between consecutive trains) per direction,
    giving riders an estimate of how long they'll wait for the next train.
    """
    with tracer.start_as_current_span("mbta.collect.predictions") as span:
        payload = mbta_get(
            "/predictions",
            {
                "filter[route]": "Orange",
                "filter[stop]":  HEADWAY_REFERENCE_STOP,
                "sort":          "arrival_time",
                "page[limit]":   "20",
            },
        )
        predictions = payload.get("data", [])

        now = datetime.now(timezone.utc)
        arrivals_by_direction: dict[str, list[datetime]] = {
            "northbound": [],
            "southbound": [],
        }

        for pred in predictions:
            attrs = pred.get("attributes", {})
            direction = DIRECTION_LABELS.get(attrs.get("direction_id"), "unknown")
            if direction == "unknown":
                continue
            # Prefer arrival_time; terminal stops only have departure_time
            time_str = attrs.get("arrival_time") or attrs.get("departure_time")
            if not time_str:
                continue
            try:
                arrival_dt = datetime.fromisoformat(time_str)
                if arrival_dt > now:
                    arrivals_by_direction[direction].append(arrival_dt)
            except (ValueError, TypeError):
                continue

        headways: dict[str, float] = {}
        for direction, arrivals in arrivals_by_direction.items():
            arrivals.sort()
            if len(arrivals) >= 2:
                gaps = [
                    (arrivals[i + 1] - arrivals[i]).total_seconds()
                    for i in range(len(arrivals) - 1)
                ]
                headways[direction] = sum(gaps) / len(gaps)
                span.set_attribute(f"mbta.headway.{direction}.seconds", headways[direction])

        _state.headway_seconds = headways
        logger.info(
            "predictions collected | headways=%s",
            {d: f"{s / 60:.1f}min" for d, s in headways.items()},
        )


# ── Poll Loop ─────────────────────────────────────────────────────────────────


def poll_once() -> None:
    """Run one collection cycle, gathering vehicles and alerts."""
    with tracer.start_as_current_span("mbta.poll") as span:
        span.set_attribute("mbta.route", "Orange")
        failed: list[str] = []

        try:
            vehicle_count = collect_vehicles()
            span.set_attribute("mbta.vehicle.count", vehicle_count)
        except Exception as exc:
            logger.error("vehicle collection failed: %s", exc)
            failed.append("vehicles")

        try:
            alert_count = collect_alerts()
            span.set_attribute("mbta.alert.count", alert_count)
        except Exception as exc:
            logger.error("alert collection failed: %s", exc)
            failed.append("alerts")

        try:
            collect_predictions()
        except Exception as exc:
            logger.error("prediction collection failed: %s", exc)
            failed.append("predictions")

        if failed:
            span.set_status(StatusCode.ERROR, f"failed collectors: {', '.join(failed)}")


def main() -> None:
    logger.info(
        "MBTA Orange Line Tracker starting | poll=%ds | otel=%s | api_key=%s",
        POLL_INTERVAL,
        OTEL_ENDPOINT,
        "set" if MBTA_API_KEY else "not set (anonymous, 20 req/min limit)",
    )

    running = True

    def _handle_stop(sig, _frame) -> None:
        nonlocal running
        logger.info("received signal %s, shutting down...", signal.Signals(sig).name)
        running = False

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    try:
        while running:
            poll_once()
            # Sleep in 1-second ticks so we can respond to shutdown signals promptly
            for _ in range(POLL_INTERVAL):
                if not running:
                    break
                time.sleep(1)
    finally:
        logger.info("flushing telemetry pipelines...")
        _tracer_provider.shutdown()
        _meter_provider.shutdown()
        logger.info("tracker stopped cleanly")


if __name__ == "__main__":
    main()
