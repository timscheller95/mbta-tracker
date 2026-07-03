# MBTA Orange Line Commute Monitor

Runs on a Raspberry Pi Zero WH. Polls the MBTA V3 API during commute hours and sends push notifications via [ntfy.sh](https://ntfy.sh) when significant Orange Line disruptions (delays >10 min, no service, suspension) are detected or resolved. Datadog APM traces are emitted when notifications fire; the native Datadog Agent monitors container uptime via the Docker socket.

## How it works

The tracker polls configurable watch windows on weekdays. Each window monitors a specific stop, direction, and time range. One notification fires when a disruption starts, one if it escalates, one when it clears. Windows reset silently if they close while a disruption is active.

## Pi Setup (one-time)

### 1. Install the native Datadog Agent

The Datadog Agent Docker image does not support ARMv6 (Pi Zero WH). Install the native Agent instead:

```sh
DD_API_KEY=<your-api-key> DD_SITE=datadoghq.com \
  bash -c "$(curl -L https://s3.amazonaws.com/dd-agent-bootstrap/datadog_agent7_raspberry.sh)"
```

Enable on boot and grant Docker socket access so the Agent can monitor the container:

```sh
sudo systemctl enable datadog-agent
sudo usermod -a -G docker dd-agent
sudo systemctl restart datadog-agent
```

The group membership and systemd enable both persist across reboots. The Agent will automatically report `docker.containers.running` and related metrics — create a Datadog monitor on that metric filtered to the `mbta-monitor` container to alert on container downtime.

### 2. Install Docker

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -a -G docker $USER   # optional: run docker without sudo
sudo systemctl enable docker      # auto-start on reboot
```

### 3. Deploy the tracker

```sh
git clone <this-repo> && cd mbta-tracker
git checkout pi-tracker
cp .env.example .env
# Fill in DD_API_KEY, NTFY_TOPIC_1, NTFY_TOPIC_2

docker compose up -d
```

The container has `restart: always` so it comes back automatically after a reboot. The container uses `network_mode: host` so `localhost:8126` reaches the native Agent's trace receiver.

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `DD_API_KEY` | required | Datadog API key (used by native Agent install and dev sidecar) |
| `NTFY_TOPIC_1` | required | ntfy.sh topic for subscriber 1 |
| `NTFY_TOPIC_2` | required | ntfy.sh topic for subscriber 2 |
| `POLL_INTERVAL_SECONDS` | `60` | How often to poll MBTA API |
| `DELAY_THRESHOLD_SECONDS` | `600` | Minimum delay (seconds) before alerting |
| `NTFY_BASE_URL` | `https://ntfy.sh` | Override for self-hosted ntfy |

Generate secure ntfy topics:
```sh
python3 -c "import secrets; print(secrets.token_hex(16))"
```

## Local dev / testing

A dev compose file runs a Datadog Agent sidecar (amd64 only — not for the Pi):

```sh
docker compose -f docker-compose.dev.yml up -d datadog-agent
docker compose -f docker-compose.dev.yml run --rm test
docker compose -f docker-compose.dev.yml down
```
