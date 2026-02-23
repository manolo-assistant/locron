# locron — Location-Aware Cron

A thin layer over [OpenClaw](https://github.com/openclaw/openclaw)'s cron that adds GPS intelligence:

- **`--tz local`** → resolves timezone from GPS. Patches the underlying cron job when you travel.
- **`--location gym --on enter`** → geofence triggers, managed by locron's daemon.
- **Regular jobs** → pass straight through to OpenClaw's cron. locron is not involved.

## How It Works

```
locron add --tz local ...
  │
  ├─ resolves GPS → timezone (e.g. "Europe/London")
  ├─ creates OpenClaw cron job with that timezone
  └─ daemon watches for tz changes → patches OpenClaw job

locron add --location gym --on enter ...
  │
  └─ daemon checks last known GPS every 30s → fires on enter/exit

locron add --tz America/New_York ...
  │
  └─ passes directly to OpenClaw cron (locron not involved)
```

## Quick Start

```bash
# Regular cron (pass-through to OpenClaw)
locron add --name cleanup --expr "0 4 * * *" --tz America/New_York --action "Clean temp files"

# Location-aware timezone — fires at 6:30 AM YOUR local time, wherever you are
locron add --name briefing --expr "30 6 * * *" --tz local --action "Morning briefing"

# Geofence trigger
locron add --name gym --location gym --on enter --action "Start gym timer"

# Geofence with separate enter/exit actions
locron add --name gym --location gym --on both --recurring \
  --action-enter "Arrived at gym" --action-exit "Left gym"

# List all jobs (OpenClaw + locron)
locron list

# Show current location & timezone
locron status

# Run daemon (system cron or standalone)
locron tick --daemon --interval 30
```

## Architecture

locron manages **only what OpenClaw can't**:

| Job type | Who schedules | Who evaluates | locron's role |
|----------|--------------|---------------|---------------|
| Regular (`--tz America/New_York`) | OpenClaw | OpenClaw | None (pass-through) |
| Local tz (`--tz local`) | OpenClaw | OpenClaw | Resolves tz, patches on change |
| Geo (`--location X`) | locron | locron | Full ownership |

### Files

| File | Purpose |
|------|---------|
| `locron.json` | locron-managed jobs (local-tz + geo) |
| `state.json` | Last known location, timezone, geo zone states |
| `locations.json` | Named geofence locations |

OpenClaw's own cron database is untouched — locron only talks to it via the API.

## Location

GPS comes from a pluggable provider (default: [Dawarich](https://github.com/Freika/dawarich)). Every tick, locron tries to fetch a new point. If the provider has nothing new (phone in low power, no signal, etc.), it uses the last known location from state. There's no "live" vs "cached" — it's always the most recent sample.

Fallback chain for timezone: GPS → last known → `LOCRON_DEFAULT_TZ`.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOCRON_DATA_DIR` | script directory | Where locron.json/state.json live |
| `LOCRON_LOCATIONS_FILE` | `$DATA_DIR/locations.json` | Named locations |
| `LOCRON_LOCATION_PROVIDER` | `dawarich` | GPS provider |
| `LOCRON_LOCATION_API_URL` | `http://localhost:3000` | Provider API URL |
| `LOCRON_LOCATION_API_KEY` | (empty) | Provider API key |
| `LOCRON_SECRETS_FILE` | (empty) | JSON file with provider credentials |
| `LOCRON_DEFAULT_TZ` | `America/New_York` | Fallback timezone |
| `LOCRON_GATEWAY_URL` | `http://127.0.0.1:18789/tools/invoke` | OpenClaw gateway |
| `LOCRON_GATEWAY_TOKEN` | (empty) | Gateway auth token |

## Dependencies

```bash
pip install tzfpy croniter
```

`tzfpy` is a Rust-based timezone finder — no numpy, no numba, no bloat.

## License

MIT
