#!/usr/bin/env python3
"""
locron — Location-aware shim for `openclaw cron`.

Sits in front of `openclaw cron` with the same interface. Peeks at args:
  --tz local       → resolves timezone from GPS, passes resolved tz to openclaw cron,
                     tracks the job so the daemon can re-patch when tz changes
  --location X     → geofence trigger, managed entirely by locron daemon
  (anything else)  → forwarded verbatim to `openclaw cron`

Usage:
  locron add --name briefing --cron "30 6 * * *" --tz local --system-event "..."
  locron add --name gym --location gym --on enter --system-event "..."
  locron add --name cleanup --cron "0 4 * * *" --tz America/New_York --system-event "..."
  locron list
  locron tick --daemon --interval 30
  locron status
"""

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from resolver import timezone_at, in_zone

log = logging.getLogger("locron")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("LOCRON_DATA_DIR", Path(__file__).resolve().parent))
LOCRON_FILE = DATA_DIR / "locron.json"
STATE_FILE = DATA_DIR / "state.json"
LOCATIONS_FILE = Path(os.environ.get("LOCRON_LOCATIONS_FILE", DATA_DIR / "locations.json"))

LOCATION_PROVIDER = os.environ.get("LOCRON_LOCATION_PROVIDER", "dawarich")
LOCATION_API_URL = os.environ.get("LOCRON_LOCATION_API_URL", "http://localhost:3000")
LOCATION_API_KEY = os.environ.get("LOCRON_LOCATION_API_KEY", "")

DEFAULT_TZ = os.environ.get("LOCRON_DEFAULT_TZ", "America/New_York")

OPENCLAW = os.environ.get("LOCRON_OPENCLAW_BIN", "openclaw")

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


# ---------------------------------------------------------------------------
# openclaw cron passthrough
# ---------------------------------------------------------------------------

def openclaw_cron(*args, capture=False) -> subprocess.CompletedProcess:
    """Run `openclaw cron <args>`."""
    cmd = [OPENCLAW, "cron"] + list(args)
    log.debug("exec: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=capture, text=True)


# ---------------------------------------------------------------------------
# Location state
# ---------------------------------------------------------------------------

def update_location(state: dict) -> dict | None:
    """Fetch latest GPS point, update state. Returns last known location."""
    fresh = _fetch_from_provider()
    if fresh:
        state["_location"] = fresh
        tz = timezone_at(fresh["lat"], fresh["lon"])
        if tz:
            state["_tz"] = tz
    return state.get("_location")


def _fetch_from_provider() -> dict | None:
    if LOCATION_PROVIDER == "dawarich":
        from provider_dawarich import get_location as dw_get
        api_key = LOCATION_API_KEY
        if not api_key:
            secrets_file = Path(os.environ.get("LOCRON_SECRETS_FILE", ""))
            if secrets_file.exists():
                secrets = load_json(secrets_file)
                api_key = secrets.get("dawarich", {}).get("api_key", "")
        return dw_get(api_url=LOCATION_API_URL, api_key=api_key)
    elif LOCATION_PROVIDER == "static":
        try:
            return {
                "lat": float(os.environ["LOCRON_STATIC_LAT"]),
                "lon": float(os.environ["LOCRON_STATIC_LON"]),
                "timestamp": int(time.time()),
            }
        except (KeyError, ValueError):
            return None
    return None


def get_current_tz(state: dict) -> str:
    return state.get("_tz", DEFAULT_TZ)


def get_current_location(state: dict) -> dict | None:
    return state.get("_location")


# ---------------------------------------------------------------------------
# Arg sniffing — detect locron-specific flags
# ---------------------------------------------------------------------------

def _has_flag(argv: list, flag: str) -> bool:
    return flag in argv


def _get_flag_value(argv: list, flag: str) -> str | None:
    try:
        idx = argv.index(flag)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    except ValueError:
        pass
    return None


def _remove_flag(argv: list, flag: str, has_value: bool = True) -> list:
    """Remove a flag (and its value) from argv."""
    result = []
    i = 0
    while i < len(argv):
        if argv[i] == flag:
            if has_value and i + 1 < len(argv):
                i += 2  # skip flag + value
            else:
                i += 1  # skip flag only
        else:
            result.append(argv[i])
            i += 1
    return result


def _replace_flag_value(argv: list, flag: str, new_value: str) -> list:
    """Replace a flag's value in argv."""
    result = []
    i = 0
    while i < len(argv):
        if argv[i] == flag and i + 1 < len(argv):
            result.append(flag)
            result.append(new_value)
            i += 2
        else:
            result.append(argv[i])
            i += 1
    return result


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.DEBUG if "-v" in sys.argv or "--verbose" in sys.argv else logging.INFO,
        format="%(asctime)s locron %(levelname)s %(message)s",
    )

    argv = sys.argv[1:]

    # Strip our own -v/--verbose (openclaw cron doesn't know it)
    argv = _remove_flag(argv, "-v", has_value=False)
    argv = _remove_flag(argv, "--verbose", has_value=False)

    if not argv:
        _print_help()
        return

    subcmd = argv[0]
    sub_argv = argv[1:]

    # locron-only subcommands
    if subcmd == "tick":
        _cmd_tick(sub_argv)
    elif subcmd == "status":
        _cmd_status()
    elif subcmd == "add" or subcmd == "create":
        _cmd_add(sub_argv)
    elif subcmd == "list":
        _cmd_list(sub_argv)
    elif subcmd == "rm" or subcmd == "remove":
        _cmd_remove(sub_argv)
    elif subcmd in ("enable", "disable", "edit", "run", "runs", "help"):
        # Pass through to openclaw cron
        openclaw_cron(subcmd, *sub_argv)
    else:
        # Unknown — let openclaw cron handle it (it'll error if invalid)
        openclaw_cron(subcmd, *sub_argv)


def _print_help():
    print("""locron — Location-aware shim for `openclaw cron`

Same interface as `openclaw cron`, plus:
  --tz local              Resolve timezone from GPS (auto-patches on travel)
  --location <name>       Geofence trigger (enter/exit named location)
  --on <enter|exit|both>  Geofence event type (default: enter)
  --recurring             Geo job fires every enter/exit cycle

Extra subcommands:
  tick [--daemon] [--interval N]   Run locron scheduler
  status                           Show GPS location & timezone

Everything else forwards to `openclaw cron`.""")


# ---------------------------------------------------------------------------
# add — the core interception point
# ---------------------------------------------------------------------------

def _cmd_add(argv: list):
    """
    Peek at argv for locron flags.
    If --location → geo job (locron manages).
    If --tz local → resolve tz, forward to openclaw cron, track for patching.
    Otherwise → forward verbatim to openclaw cron.
    """
    location = _get_flag_value(argv, "--location")
    tz = _get_flag_value(argv, "--tz")

    if location:
        _add_geo(argv)
    elif tz == "local":
        _add_local_tz(argv)
    else:
        # Pure pass-through
        openclaw_cron("add", *argv)


def _add_local_tz(argv: list):
    """Resolve tz from GPS, replace --tz local with resolved tz, forward to openclaw cron."""
    state = load_json(STATE_FILE, {})
    update_location(state)
    save_json(STATE_FILE, state)
    resolved_tz = get_current_tz(state)

    # Replace --tz local → --tz <resolved>
    forwarded = _replace_flag_value(argv, "--tz", resolved_tz)

    # Add --json to capture the job ID
    result = openclaw_cron("add", *forwarded, "--json", capture=True)
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    # Parse job ID from output
    job_id = ""
    name = _get_flag_value(argv, "--name") or ""
    expr = _get_flag_value(argv, "--cron") or ""
    try:
        data = json.loads(result.stdout)
        job_id = data.get("id", "")
    except (json.JSONDecodeError, TypeError):
        pass

    # Track in locron.json
    if job_id:
        locron_jobs = load_json(LOCRON_FILE, [])
        locron_jobs = [j for j in locron_jobs if j.get("name") != name]
        locron_jobs.append({
            "name": name,
            "kind": "local_tz",
            "openclaw_id": job_id,
            "expr": expr,
            "current_tz": resolved_tz,
        })
        save_json(LOCRON_FILE, locron_jobs)
        log.info("Tracking local-tz job: %s (id=%s, tz=%s)", name, job_id, resolved_tz)


def _add_geo(argv: list):
    """Add a geofence job to locron.json."""
    name = _get_flag_value(argv, "--name") or ""
    location = _get_flag_value(argv, "--location") or ""
    on = _get_flag_value(argv, "--on") or "enter"
    recurring = _has_flag(argv, "--recurring")
    action = _get_flag_value(argv, "--system-event") or _get_flag_value(argv, "--message") or ""
    spawn = bool(_get_flag_value(argv, "--message"))  # --message = agent turn = spawn

    locron_jobs = load_json(LOCRON_FILE, [])
    locron_jobs = [j for j in locron_jobs if j.get("name") != name]

    job = {
        "name": name,
        "kind": "geo",
        "enabled": True,
        "recurring": recurring,
        "trigger": {"location": location, "on": on},
        "action": {"text": action, "spawn": spawn},
    }
    locron_jobs.append(job)
    save_json(LOCRON_FILE, locron_jobs)
    print(f"Added geo job: {name} (location={location}, on={on})")


# ---------------------------------------------------------------------------
# list — merge openclaw cron list + locron geo jobs
# ---------------------------------------------------------------------------

def _cmd_list(argv: list):
    """Show all jobs: openclaw cron + locron geo."""
    # Get openclaw jobs
    result = openclaw_cron("list", *argv, capture=True)
    print(result.stdout, end="")

    # Append locron geo jobs
    locron_jobs = load_json(LOCRON_FILE, [])
    geo_jobs = [j for j in locron_jobs if j.get("kind") == "geo"]
    if geo_jobs:
        print("\n── locron geo jobs ──")
        for j in geo_jobs:
            enabled = "✅" if j.get("enabled", True) else "❌"
            t = j.get("trigger", {})
            recur = "recurring" if j.get("recurring") else "one-shot"
            print(f"  {enabled} {j.get('name', '?'):30s} | geo:{t.get('location')} on:{t.get('on')} ({recur})")


# ---------------------------------------------------------------------------
# remove — check locron.json too
# ---------------------------------------------------------------------------

def _cmd_remove(argv: list):
    """Remove from locron.json if tracked, then forward to openclaw cron."""
    name = _get_flag_value(argv, "--name")
    if name:
        locron_jobs = load_json(LOCRON_FILE, [])
        found = [j for j in locron_jobs if j.get("name") == name]
        if found:
            j = found[0]
            if j.get("kind") == "geo":
                # Pure locron job — just remove from locron.json
                locron_jobs = [x for x in locron_jobs if x.get("name") != name]
                save_json(LOCRON_FILE, locron_jobs)
                print(f"Removed geo job: {name}")
                return
            else:
                # local_tz — remove tracking, fall through to openclaw rm
                locron_jobs = [x for x in locron_jobs if x.get("name") != name]
                save_json(LOCRON_FILE, locron_jobs)

    # Forward to openclaw cron rm
    openclaw_cron("rm", *argv)


# ---------------------------------------------------------------------------
# tick — daemon loop
# ---------------------------------------------------------------------------

def _cmd_tick(argv: list):
    daemon = _has_flag(argv, "--daemon")
    interval = int(_get_flag_value(argv, "--interval") or 30)

    if daemon:
        log.info("locron daemon (interval=%ds)", interval)
        while True:
            try:
                _tick()
            except Exception:
                log.exception("Tick failed")
            time.sleep(interval)
    else:
        _tick()


def _tick():
    """One scheduler tick: update location, patch tz jobs, evaluate geo triggers."""
    state = load_json(STATE_FILE, {})
    locron_jobs = load_json(LOCRON_FILE, [])

    if not locron_jobs:
        return

    locations = load_json(LOCATIONS_FILE, {})
    update_location(state)
    current_tz = get_current_tz(state)
    loc = get_current_location(state)

    dirty = False

    for job in locron_jobs:
        name = job.get("name", "")
        kind = job.get("kind")

        if kind == "local_tz":
            # Patch OpenClaw cron job if timezone changed
            # Only patch if we have a GPS-derived tz (not fallback default)
            has_gps_tz = "_tz" in state
            if has_gps_tz and job.get("current_tz") != current_tz:
                oc_id = job.get("openclaw_id")
                if oc_id:
                    log.info("Tz changed for %s: %s → %s", name,
                             job.get("current_tz"), current_tz)
                    openclaw_cron("edit", oc_id, "--tz", current_tz)
                    job["current_tz"] = current_tz
                    dirty = True

        elif kind == "geo":
            if not job.get("enabled", True) or not loc:
                continue

            job_state = state.setdefault(f"_geo:{name}", {})
            event = _eval_geo(job, job_state, loc, locations)

            if event:
                log.info("FIRE [geo:%s] %s", event, name)
                _fire_geo(job, event, name)

                job_state["last_fired"] = datetime.now(ZoneInfo("UTC")).isoformat()
                job_state["fire_count"] = job_state.get("fire_count", 0) + 1
                dirty = True

                if not job.get("recurring", False):
                    job["enabled"] = False
                    dirty = True

    if dirty:
        save_json(LOCRON_FILE, locron_jobs)
    save_json(STATE_FILE, state)


def _eval_geo(job: dict, job_state: dict, loc: dict, locations: dict) -> str | None:
    """Evaluate geofence trigger. Returns 'enter', 'exit', or None."""
    trigger = job.get("trigger", {})
    zone_name = trigger.get("location")
    zone = locations.get(zone_name) if zone_name else None
    if not zone:
        # Try inline coords
        if "lat" in trigger and "lon" in trigger:
            zone = {"lat": trigger["lat"], "lon": trigger["lon"],
                    "radius_m": trigger.get("radius_m", 50)}
    if not zone:
        return None

    currently_inside = in_zone(loc["lat"], loc["lon"], zone)
    was_inside = job_state.get("in_zone", False)
    job_state["in_zone"] = currently_inside

    on = trigger.get("on", "enter")
    event = None
    if currently_inside and not was_inside:
        event = "enter"
    elif not currently_inside and was_inside:
        event = "exit"

    if event and (on == "both" or on == event):
        return event
    return None


def _fire_geo(job: dict, event: str, name: str):
    """Fire a geo job's action via openclaw cron run or direct spawn."""
    action = job.get("action", {})
    text = action.get("text", "")
    if not text:
        return

    if action.get("spawn"):
        # Use openclaw to spawn a sub-agent
        subprocess.run([
            OPENCLAW, "cron", "add",
            "--name", f"locron-geo-{name}-{int(time.time())}",
            "--at", "+0s",
            "--delete-after-run",
            "--session", "isolated",
            "--message", text,
            "--announce",
        ])
    else:
        # Inject system event into main session
        subprocess.run([
            OPENCLAW, "cron", "add",
            "--name", f"locron-geo-{name}-{int(time.time())}",
            "--at", "+0s",
            "--delete-after-run",
            "--system-event", text,
        ])


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _cmd_status():
    state = load_json(STATE_FILE, {})
    update_location(state)
    save_json(STATE_FILE, state)

    loc = get_current_location(state)
    tz = get_current_tz(state)

    if loc:
        print(f"📍 Location: ({loc['lat']:.4f}, {loc['lon']:.4f})")
    else:
        print(f"📍 No location yet")
    print(f"🕐 Timezone: {tz}")
    print(f"🕐 Local time: {datetime.now(ZoneInfo(tz)).strftime('%Y-%m-%d %H:%M:%S %Z')}")

    locron_jobs = load_json(LOCRON_FILE, [])
    local_tz = [j for j in locron_jobs if j.get("kind") == "local_tz"]
    geo = [j for j in locron_jobs if j.get("kind") == "geo"]
    print(f"📋 Tracked: {len(local_tz)} local-tz, {len(geo)} geo")


if __name__ == "__main__":
    main()
