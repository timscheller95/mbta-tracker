# MBTA Orange Line Tracker

Real-time telemetry pipeline for the MBTA Orange Line, demonstrating production-grade observability with **OpenTelemetry**, **Grafana**, and **Datadog**.

## Architecture

```
                        ┌─────────────────────────────┐
                        │       MBTA V3 REST API       │
                        │  api-v3.mbta.com             │
                        └────────────┬────────────────┘
                                     │ polls every 30s
                        ┌────────────▼────────────────┐
                        │     Python Tracker Service   │
                        │  · OTel traces (OTLP/HTTP)  │
                        │  · OTel metrics (OTLP/HTTP) │
                        └────────────┬────────────────┘
                                     │ :4318
                        ┌────────────▼────────────────┐
                        │    OpenTelemetry Collector   │
                        │         (Contrib)            │
                        └────┬──────────────┬─────────┘
                             │              │
               ┌─────────────▼──┐    ┌──────▼──────────┐
               │   Prometheus   │    │    Datadog       │
               │   :8889/scrape │    │  (opt-in)        │
               └───────┬────────┘    └─────────────────┘
                       │
               ┌───────▼────────┐
               │    Grafana     │
               │   :3000        │
               └────────────────┘
```

## Quick Start

```bash
cp .env.example .env
# Optionally set MBTA_API_KEY and/or DD_API_KEY in .env

docker compose up -d

# Grafana      → http://localhost:3000  (admin / mbta-tracker)
# Prometheus   → http://localhost:9090
# OTel health  → http://localhost:13133
```

The Grafana dashboard is auto-provisioned under **MBTA → MBTA Orange Line Tracker**.

## Prerequisites

- Docker + Docker Compose
- MBTA API key — [register free](https://api-v3.mbta.com/) to raise the anonymous rate limit (20 req/min)
- Datadog account + API key (optional — see below)

## Metrics Collected

| OTel metric | Prometheus name | Description |
|---|---|---|
| `mbta.vehicle.count` | `mbta_vehicle_count` | Active vehicles by direction |
| `mbta.vehicle.status` | `mbta_vehicle_status` | Vehicles by stop status |
| `mbta.vehicle.occupancy` | `mbta_vehicle_occupancy` | Vehicles by occupancy level |
| `mbta.vehicle.speed` | `mbta_vehicle_speed_mph` | Speed histogram (mph) |
| `mbta.vehicle.latitude` | `mbta_vehicle_latitude_deg` | Per-vehicle GPS latitude |
| `mbta.vehicle.longitude` | `mbta_vehicle_longitude_deg` | Per-vehicle GPS longitude |
| `mbta.alert.count` | `mbta_alert_count` | Active alerts total |
| `mbta.alert.by_effect` | `mbta_alert_by_effect` | Alert counts by effect type and direction |
| `mbta.alert.info` | `mbta_alert_info` | Info metric — one series per alert with human-readable `header` label |
| `mbta.stop.alert` | `mbta_stop_alert` | Affected stations (1 = currently impacted) |
| `mbta.headway` | `mbta_headway_seconds` | Average gap between trains at Downtown Crossing |
| `mbta.api.request.duration` | `mbta_api_request_duration_seconds` | MBTA API latency histogram |
| `mbta.api.requests` | `mbta_api_requests_total` | API requests by endpoint and status code |
| `mbta.api.errors` | `mbta_api_errors_total` | API errors by endpoint and error type |

Traces are also emitted: one parent span per poll cycle, one client span per MBTA API call (HTTP semantic conventions).

## Datadog

Datadog export is **disabled by default**. To enable it:

1. Set `DD_API_KEY` (and optionally `DD_SITE`) in `.env`
2. Uncomment the `datadog` exporter block in `otel-collector/config.yaml` and add it to the pipelines

The Collector ships metrics as Datadog distributions (percentile-queryable) and traces with OTel span names.

## OTel Design Notes

- **Resource attributes** (`service.name`, `service.namespace`, `deployment.environment`, `mbta.route`) are promoted to Prometheus labels via `resource_to_telemetry_conversion`
- **Observable gauges** use a callback pattern — shared `_State` is updated each poll cycle; the OTel SDK calls the callbacks on each metric export interval
- **Info metric pattern** — `mbta.alert.info` is always value 1 with alert metadata as labels, enabling Grafana table panels to display free-text alert descriptions
- **Graceful shutdown** — SIGTERM flushes the `BatchSpanProcessor` and `PeriodicExportingMetricReader` before exit

## Project Structure

```
mbta-tracker/
├── tracker/
│   ├── main.py               # OTel-instrumented MBTA poller
│   ├── requirements.txt
│   ├── requirements-dev.txt
│   ├── Dockerfile
│   ├── pytest.ini
│   ├── conftest.py
│   └── tests/
│       └── test_main.py      # 69 unit tests
├── otel-collector/
│   └── config.yaml           # Receiver + dual-ship pipeline
├── prometheus/
│   └── prometheus.yml
├── grafana/
│   └── provisioning/
│       ├── datasources/
│       └── dashboards/
├── docker-compose.yml
├── .env.example
└── README.md
```

## Running Tests

```bash
cd tracker
pip install -r requirements-dev.txt
pytest
```
