# MBTA Orange Line Commute Monitor

Runs on a Raspberry Pi Zero WH. Polls the MBTA V3 API during commute hours and sends push notifications via [ntfy.sh](https://ntfy.sh) when significant Orange Line disruptions (delays >10 min, no service, suspension) are detected or resolved. Uptime is monitored via [healthchecks.io](https://healthchecks.io), which notifies via ntfy if the app stops checking in.

## How it works

The tracker polls configurable watch windows on weekdays. Each window monitors a specific stop, direction, and time range. One notification fires when a disruption starts, one if it escalates, one when it clears. Windows reset silently if they close while a disruption is active.

After each poll the app pings a healthchecks.io URL. If pings stop arriving (crash, Pi offline, Docker stopped), healthchecks.io sends an alert to ntfy topic 2.

## Pi Setup (one-time)

### 1. Install Docker

```sh
curl -fsSL https://get.docker.com | sh
sudo systemctl enable docker
```

### 2. Deploy the tracker

```sh
git clone <this-repo> && cd mbta-tracker
git checkout pi-tracker
cp .env.example .env
# Fill in NTFY_TOPIC_1, NTFY_TOPIC_2, HEALTHCHECKS_URL

docker compose up -d
```

The container has `restart: always` — it comes back automatically after a reboot. Docker is enabled on boot via `systemctl enable docker`.

### 3. Set up healthchecks.io

1. Create a free account at [healthchecks.io](https://healthchecks.io)
2. Add a new check — set the period to match `POLL_INTERVAL_SECONDS` (default 60s) and grace period to ~5 minutes
3. Under **Integrations**, add ntfy.sh pointed at your topic 2 URL
4. Copy the ping URL into `HEALTHCHECKS_URL` in `.env`

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `NTFY_TOPIC_1` | required | ntfy.sh topic for subscriber 1 |
| `NTFY_TOPIC_2` | required | ntfy.sh topic for subscriber 2 (also receives uptime alerts) |
| `HEALTHCHECKS_URL` | required | healthchecks.io ping URL |
| `POLL_INTERVAL_SECONDS` | `60` | How often to poll MBTA API |
| `DELAY_THRESHOLD_SECONDS` | `600` | Minimum delay (seconds) before alerting |
| `NTFY_BASE_URL` | `https://ntfy.sh` | Override for self-hosted ntfy |

Generate secure ntfy topics:
```sh
python3 -c "import secrets; print(secrets.token_hex(16))"
```

## Local dev / testing

```sh
docker compose -f docker-compose.dev.yml run --rm test
```
