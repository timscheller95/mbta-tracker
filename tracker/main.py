"""
MBTA Orange Line Commute Monitor
=================================
Polls the MBTA V3 API for Orange Line disruptions at specific stations during
commute hours and sends ntfy.sh push notifications when significant service
issues (>10 min delays, no service) are detected or resolved.

OpenTelemetry auto-instrumentation ships traces to Datadog APM for uptime
monitoring. Notifications are handled entirely via ntfy.sh — no metrics are
shipped to Datadog.
"""

import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# ── Configuration ─────────────────────────────────────────────────────────────

DD_SAT = os.environ["DD_SAT"]
DD_SITE = os.getenv("DD_SITE", "datadoghq.com")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
NTFY_BASE = os.getenv("NTFY_BASE_URL", "https://ntfy.sh")
NTFY_TOPIC_WIFE = os.environ["NTFY_TOPIC_WIFE"]
NTFY_TOPIC_SELF = os.environ["NTFY_TOPIC_SELF"]
DELAY_THRESHOLD_S = int(os.getenv("DELAY_THRESHOLD_SECONDS", "600"))  # 10 min

# OTLP endpoint for Datadog APM. Override via OTEL_EXPORTER_OTLP_ENDPOINT if needed.
# For US1 this resolves to https://api.datadoghq.com/api/intake/otlp/v1/traces
OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    f"https://api.{DD_SITE}/api/intake/otlp",
)

ET = ZoneInfo("America/New_York")

# Effects that constitute a service disruption for commuters.
# Excludes elevator/escalator closures, policy changes, and general notices.
DISRUPTION_EFFECTS = {"DELAY", "NO_SERVICE", "SUSPENSION", "STOP_CLOSURE"}
ACTIVE_LIFECYCLES = {"NEW", "ONGOING", "ONGOING_UPCOMING"}

# Ordered most-to-least severe — used to detect escalation
SEVERITY = ["NO_SERVICE", "SUSPENSION", "STOP_CLOSURE", "DELAY"]

# ── OTel / APM ────────────────────────────────────────────────────────────────

_resource = Resource.create({SERVICE_NAME: "mbta-commute-monitor"})
_trace_exporter = OTLPSpanExporter(
    endpoint=OTLP_ENDPOINT,
    headers={"Authorization": f"Bearer {DD_SAT}"},
)
_tracer_provider = TracerProvider(resource=_resource)
_tracer_provider.add_span_processor(BatchSpanProcessor(_trace_exporter))
trace.set_tracer_provider(_tracer_provider)

# Auto-instrument requests — every HTTP call (MBTA API + ntfy.sh) becomes a trace span
RequestsInstrumentor().instrument()

tracer = trace.get_tracer(__name__)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("mbta.monitor")

# ── Watch Windows ─────────────────────────────────────────────────────────────


@dataclass
class WatchWindow:
    name: str
    stop_id: str
    stop_name: str
    direction_id: int       # 0 = southbound (Forest Hills), 1 = northbound (Oak Grove)
    direction_label: str    # e.g. "northbound towards Oak Grove"
    start_hour: int         # ET hour, inclusive
    end_hour: int           # ET hour, exclusive
    ntfy_topic: str
    # Runtime state — persists across polls, resets at window close or resolution
    disrupted: bool = field(default=False)
    disruption_effect: str = field(default="")


WATCH_WINDOWS = [
    # ── Wife ────────────────────────────────────────────────────────────────
    WatchWindow(
        name="wife_morning",
        stop_id="place-forhl",
        stop_name="Forest Hills",
        direction_id=1,
        direction_label="northbound towards Oak Grove",
        start_hour=8,
        end_hour=10,
        ntfy_topic=NTFY_TOPIC_WIFE,
    ),
    WatchWindow(
        name="wife_evening",
        stop_id="place-masta",
        stop_name="Massachusetts Ave",
        direction_id=0,
        direction_label="southbound towards Forest Hills",
        start_hour=16,
        end_hour=19,
        ntfy_topic=NTFY_TOPIC_WIFE,
    ),
    # ── Tim ─────────────────────────────────────────────────────────────────
    WatchWindow(
        name="tim_morning",
        stop_id="place-masta",
        stop_name="Massachusetts Ave",
        direction_id=1,
        direction_label="northbound towards Oak Grove",
        start_hour=8,
        end_hour=12,
        ntfy_topic=NTFY_TOPIC_SELF,
    ),
    WatchWindow(
        name="tim_afternoon",
        stop_id="place-state",
        stop_name="State",
        direction_id=0,
        direction_label="southbound towards Forest Hills",
        start_hour=14,
        end_hour=17,
        ntfy_topic=NTFY_TOPIC_SELF,
    ),
]

# ── MBTA API ──────────────────────────────────────────────────────────────────

_mbta = requests.Session()
_mbta.headers.update({"Accept": "application/vnd.api+json"})


def _check_alerts(stop_id: str, direction_id: int) -> str | None:
    """Returns the most severe active disruption effect from MBTA alerts, or None."""
    data = _mbta.get(
        "https://api-v3.mbta.com/alerts",
        params={
            "filter[route]": "Orange",
            "filter[stop]": stop_id,
            "filter[direction_id]": direction_id,
            "filter[activity]": "BOARD,EXIT,RIDE",
        },
        timeout=10,
    ).json()

    found: set[str] = set()
    for alert in data.get("data", []):
        attrs = alert.get("attributes", {})
        if attrs.get("lifecycle") not in ACTIVE_LIFECYCLES:
            continue
        effect = attrs.get("effect", "")
        if effect in DISRUPTION_EFFECTS:
            found.add(effect)

    for effect in SEVERITY:
        if effect in found:
            return effect
    return None


def _check_prediction_delay(stop_id: str, direction_id: int) -> bool:
    """Returns True if the next predicted train is delayed beyond the threshold."""
    data = _mbta.get(
        "https://api-v3.mbta.com/predictions",
        params={
            "filter[route]": "Orange",
            "filter[stop]": stop_id,
            "filter[direction_id]": direction_id,
            "include": "schedule",
            "sort": "arrival_time",
            "page[limit]": "5",
        },
        timeout=10,
    ).json()

    schedules: dict[str, str | None] = {
        item["id"]: item.get("attributes", {}).get("arrival_time")
        for item in data.get("included", [])
        if item.get("type") == "schedule"
    }

    now = datetime.now(ZoneInfo("UTC"))
    for pred in data.get("data", []):
        attrs = pred.get("attributes", {})
        time_str = attrs.get("arrival_time") or attrs.get("departure_time")
        if not time_str:
            continue
        try:
            arrival_dt = datetime.fromisoformat(time_str)
        except (ValueError, TypeError):
            continue
        if (arrival_dt - now).total_seconds() < 0:
            continue  # already passed

        sched_rel = pred.get("relationships", {}).get("schedule", {}).get("data")
        if sched_rel:
            sched_arrival = schedules.get(sched_rel.get("id"))
            if sched_arrival:
                try:
                    delay_s = (arrival_dt - datetime.fromisoformat(sched_arrival)).total_seconds()
                    return delay_s >= DELAY_THRESHOLD_S
                except (ValueError, TypeError):
                    pass
        break  # only examine the soonest upcoming train

    return False


def get_disruption(stop_id: str, direction_id: int) -> str | None:
    """
    Returns the current disruption effect at this stop/direction, or None if clear.
    Official MBTA alerts take priority; prediction-based delay is the fallback.
    """
    effect = _check_alerts(stop_id, direction_id)
    if effect:
        return effect
    if _check_prediction_delay(stop_id, direction_id):
        return "DELAY"
    return None


# ── ntfy.sh ───────────────────────────────────────────────────────────────────

_ntfy = requests.Session()

_EFFECT_LABEL: dict[str, str] = {
    "DELAY": "significant delays (>10 min)",
    "NO_SERVICE": "no service",
    "SUSPENSION": "service suspended",
    "STOP_CLOSURE": "stop closed",
}


def _ntfy_post(topic: str, title: str, body: str, priority: str, tags: list[str]) -> None:
    _ntfy.post(
        f"{NTFY_BASE}/{topic}",
        data=body.encode(),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": ",".join(tags),
        },
        timeout=10,
    ).raise_for_status()
    logger.info("ntfy sent | topic=%s | %s", topic, title)


def notify_disruption(window: WatchWindow, effect: str) -> None:
    label = _EFFECT_LABEL.get(effect, effect.lower().replace("_", " "))
    _ntfy_post(
        topic=window.ntfy_topic,
        title=f"Orange Line Alert — {window.stop_name}",
        body=f"Trains {window.direction_label} from {window.stop_name} are experiencing {label}.",
        priority="high",
        tags=["rotating_light", "orange_circle"],
    )


def notify_escalation(window: WatchWindow, new_effect: str) -> None:
    label = _EFFECT_LABEL.get(new_effect, new_effect.lower().replace("_", " "))
    _ntfy_post(
        topic=window.ntfy_topic,
        title=f"Orange Line Update — {window.stop_name}",
        body=f"Update: trains {window.direction_label} from {window.stop_name} — now showing {label}.",
        priority="high",
        tags=["warning", "orange_circle"],
    )


def notify_resolved(window: WatchWindow) -> None:
    _ntfy_post(
        topic=window.ntfy_topic,
        title=f"Orange Line Cleared — {window.stop_name}",
        body=f"Trains {window.direction_label} from {window.stop_name} appear to be running normally again.",
        priority="default",
        tags=["white_check_mark", "orange_circle"],
    )


# ── Window Evaluation ─────────────────────────────────────────────────────────


def is_active_window(window: WatchWindow, now_et: datetime) -> bool:
    """True on weekdays within the window's configured hour range."""
    return now_et.weekday() < 5 and window.start_hour <= now_et.hour < window.end_hour


def evaluate_window(window: WatchWindow, now_et: datetime) -> None:
    if not is_active_window(window, now_et):
        if window.disrupted:
            # Window closed while a disruption was active — reset silently
            logger.info("[%s] window closed while disrupted — resetting state", window.name)
            window.disrupted = False
            window.disruption_effect = ""
        return

    with tracer.start_as_current_span(f"evaluate.{window.name}"):
        try:
            effect = get_disruption(window.stop_id, window.direction_id)
        except Exception as exc:
            logger.error("[%s] MBTA query failed: %s", window.name, exc)
            return

        if effect and not window.disrupted:
            # New disruption detected
            window.disrupted = True
            window.disruption_effect = effect
            try:
                notify_disruption(window, effect)
            except Exception as exc:
                logger.error("[%s] ntfy failed (disruption): %s", window.name, exc)

        elif effect and window.disrupted and effect != window.disruption_effect:
            # Disruption escalated or changed character
            window.disruption_effect = effect
            try:
                notify_escalation(window, effect)
            except Exception as exc:
                logger.error("[%s] ntfy failed (escalation): %s", window.name, exc)

        elif not effect and window.disrupted:
            # Disruption cleared within the active window
            window.disrupted = False
            window.disruption_effect = ""
            try:
                notify_resolved(window)
            except Exception as exc:
                logger.error("[%s] ntfy failed (resolution): %s", window.name, exc)

        else:
            logger.info(
                "[%s] no change | disrupted=%s effect=%s",
                window.name,
                window.disrupted,
                window.disruption_effect or "none",
            )


# ── Poll Loop ─────────────────────────────────────────────────────────────────


def poll_once() -> None:
    now_et = datetime.now(ET)
    with tracer.start_as_current_span("mbta.poll"):
        for window in WATCH_WINDOWS:
            evaluate_window(window, now_et)


def main() -> None:
    logger.info(
        "MBTA commute monitor starting | poll=%ds | otlp=%s | sat=%s...",
        POLL_INTERVAL,
        OTLP_ENDPOINT,
        DD_SAT[:8],
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
            for _ in range(POLL_INTERVAL):
                if not running:
                    break
                time.sleep(1)
    finally:
        logger.info("flushing traces to Datadog APM...")
        _tracer_provider.shutdown()
        logger.info("stopped")


if __name__ == "__main__":
    main()
