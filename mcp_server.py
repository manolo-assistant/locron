#!/usr/bin/env python3
"""
locron MCP server — Location-aware cron, transparent over OpenClaw's cron.

Exposes tools: list, add, remove, update, enable, disable, run, status
All calls proxy to `openclaw cron` unless locron-specific flags are present.

Protocol: MCP (Model Context Protocol) over stdio (JSON-RPC 2.0)
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

log = logging.getLogger("locron-mcp")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("LOCRON_DATA_DIR", Path(__file__).resolve().parent))
LOCRON_FILE = DATA_DIR / "locron.json"
STATE_FILE = DATA_DIR / "state.json"
LOCATIONS_FILE = Path(os.environ.get("LOCRON_LOCATIONS_FILE",
                                      "/root/clawd/data/locations.json"))

LOCATION_PROVIDER = os.environ.get("LOCRON_LOCATION_PROVIDER", "dawarich")
LOCATION_API_URL = os.environ.get("LOCRON_LOCATION_API_URL", "http://localhost:3000")
LOCATION_API_KEY = os.environ.get("LOCRON_LOCATION_API_KEY", "")

DEFAULT_TZ = os.environ.get("LOCRON_DEFAULT_TZ", "America/New_York")
OPENCLAW = os.environ.get("LOCRON_OPENCLAW_BIN", "openclaw")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}

def save_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2, default=str) + "\n")

def openclaw_cron(*args) -> dict:
    """Run `openclaw cron <args> --json` and parse output."""
    cmd = [OPENCLAW, "cron"] + list(args) + ["--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.stdout.strip():
            return json.loads(r.stdout)
        return {"error": r.stderr.strip() or "empty response"}
    except json.JSONDecodeError:
        return {"output": r.stdout.strip(), "stderr": r.stderr.strip()}
    except Exception as e:
        return {"error": str(e)}

def openclaw_cron_raw(*args) -> str:
    """Run `openclaw cron <args>` and return raw output."""
    cmd = [OPENCLAW, "cron"] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.stdout + r.stderr
    except Exception as e:
        return str(e)

# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

def update_location(state):
    fresh = _fetch_from_provider()
    if fresh:
        state["_location"] = fresh
        tz = timezone_at(fresh["lat"], fresh["lon"])
        if tz:
            state["_tz"] = tz
    return state.get("_location")

def _fetch_from_provider():
    if LOCATION_PROVIDER == "dawarich":
        from provider_dawarich import get_location as dw_get
        api_key = LOCATION_API_KEY
        if not api_key:
            sf = Path(os.environ.get("LOCRON_SECRETS_FILE", ""))
            if sf.exists():
                secrets = load_json(sf)
                api_key = secrets.get("dawarich", {}).get("api_key", "")
        return dw_get(api_url=LOCATION_API_URL, api_key=api_key)
    elif LOCATION_PROVIDER == "static":
        try:
            return {"lat": float(os.environ["LOCRON_STATIC_LAT"]),
                    "lon": float(os.environ["LOCRON_STATIC_LON"]),
                    "timestamp": int(time.time())}
        except (KeyError, ValueError):
            return None
    return None

def get_current_tz(state):
    return state.get("_tz", DEFAULT_TZ)

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

TOOLS = {}

def tool(name, description, schema):
    """Decorator to register a tool."""
    def decorator(fn):
        TOOLS[name] = {"fn": fn, "description": description, "schema": schema}
        return fn
    return decorator


@tool("list", "List all cron jobs (OpenClaw + locron geo jobs)", {
    "type": "object",
    "properties": {
        "includeDisabled": {"type": "boolean", "description": "Include disabled jobs"}
    }
})
def tool_list(params):
    args = ["list"]
    if params.get("includeDisabled"):
        args.append("--include-disabled")
    result = openclaw_cron(*args)

    # Merge in locron geo jobs
    locron_jobs = load_json(LOCRON_FILE, [])
    geo_jobs = [j for j in locron_jobs if j.get("kind") == "geo"]

    # Mark local-tz jobs in the openclaw output
    local_tz_jobs = {j["openclaw_id"]: j for j in locron_jobs
                     if j.get("kind") == "local_tz" and j.get("openclaw_id")}

    oc_jobs = result.get("jobs", [])
    for j in oc_jobs:
        if j.get("id") in local_tz_jobs:
            lt = local_tz_jobs[j["id"]]
            j["_locron"] = {"tz": "local", "resolved": lt.get("current_tz")}

    return {
        "jobs": oc_jobs,
        "geo_jobs": geo_jobs,
        "_resolved_tz": get_current_tz(load_json(STATE_FILE, {})),
    }


@tool("add", "Add a cron job. Use tz='local' for GPS-aware timezone. Use location for geofence triggers.", {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Job name"},
        "cron": {"type": "string", "description": "Cron expression (5-field)"},
        "tz": {"type": "string", "description": "'local' for GPS-resolved, or IANA timezone"},
        "at": {"type": "string", "description": "One-shot: ISO timestamp or +duration (e.g. +20m)"},
        "every": {"type": "string", "description": "Recurring interval (e.g. 10m, 1h)"},
        "systemEvent": {"type": "string", "description": "System event text (main session)"},
        "message": {"type": "string", "description": "Agent message (isolated session)"},
        "session": {"type": "string", "enum": ["main", "isolated"], "description": "Session target"},
        "deleteAfterRun": {"type": "boolean"},
        "announce": {"type": "boolean", "description": "Announce result to chat"},
        "wake": {"type": "string", "enum": ["now", "next-heartbeat"]},
        "location": {"type": "string", "description": "Geofence: named location from locations.json"},
        "on": {"type": "string", "enum": ["enter", "exit", "both"], "description": "Geofence event type"},
        "recurring": {"type": "boolean", "description": "Geo job fires every cycle"},
        "spawn": {"type": "boolean", "description": "Geo: spawn sub-agent instead of system event"},
    },
    "required": ["name"],
})
def tool_add(params):
    location = params.get("location")
    tz = params.get("tz")

    if location:
        return _add_geo(params)
    elif tz == "local":
        return _add_local_tz(params)
    else:
        return _add_passthrough(params)


def _add_passthrough(params):
    """Forward to openclaw cron add."""
    args = _params_to_cli_args(params)
    return openclaw_cron("add", *args)


def _add_local_tz(params):
    """Resolve tz from GPS, create in openclaw cron, track in locron.json."""
    state = load_json(STATE_FILE, {})
    update_location(state)
    save_json(STATE_FILE, state)
    resolved_tz = get_current_tz(state)

    # Forward to openclaw with resolved tz
    params_copy = dict(params)
    params_copy["tz"] = resolved_tz
    args = _params_to_cli_args(params_copy)
    result = openclaw_cron("add", *args)

    job_id = result.get("id", "")
    if job_id:
        locron_jobs = load_json(LOCRON_FILE, [])
        locron_jobs = [j for j in locron_jobs if j.get("name") != params["name"]]
        locron_jobs.append({
            "name": params["name"],
            "kind": "local_tz",
            "openclaw_id": job_id,
            "expr": params.get("cron", ""),
            "current_tz": resolved_tz,
        })
        save_json(LOCRON_FILE, locron_jobs)

    result["_locron"] = {"tz": "local", "resolved": resolved_tz}
    return result


def _add_geo(params):
    """Store geo job in locron.json."""
    locron_jobs = load_json(LOCRON_FILE, [])
    locron_jobs = [j for j in locron_jobs if j.get("name") != params["name"]]

    job = {
        "name": params["name"],
        "kind": "geo",
        "enabled": True,
        "recurring": params.get("recurring", False),
        "trigger": {
            "location": params["location"],
            "on": params.get("on", "enter"),
        },
        "action": {
            "text": params.get("systemEvent") or params.get("message") or "",
            "spawn": params.get("spawn", False),
        },
    }
    locron_jobs.append(job)
    save_json(LOCRON_FILE, locron_jobs)
    return {"ok": True, "name": params["name"], "kind": "geo",
            "location": params["location"], "on": params.get("on", "enter")}


@tool("remove", "Remove a cron job (by name or id)", {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "jobId": {"type": "string"},
    },
})
def tool_remove(params):
    name = params.get("name")
    job_id = params.get("jobId")

    # Check locron.json
    locron_jobs = load_json(LOCRON_FILE, [])
    found = None
    for j in locron_jobs:
        if (name and j.get("name") == name) or (job_id and j.get("openclaw_id") == job_id):
            found = j
            break

    if found:
        if found.get("kind") == "geo":
            locron_jobs = [j for j in locron_jobs if j.get("name") != found["name"]]
            save_json(LOCRON_FILE, locron_jobs)
            return {"ok": True, "removed": found["name"], "kind": "geo"}
        else:
            # local_tz — remove tracking, also remove from openclaw
            oc_id = found.get("openclaw_id")
            locron_jobs = [j for j in locron_jobs if j.get("name") != found["name"]]
            save_json(LOCRON_FILE, locron_jobs)
            if oc_id:
                openclaw_cron("rm", oc_id)
            return {"ok": True, "removed": found["name"], "kind": "local_tz"}

    # Not in locron — forward to openclaw
    jid = _resolve_job_id(name, job_id)
    if not jid:
        return {"error": f"Job not found: {name or job_id}"}
    return openclaw_cron("rm", jid)


@tool("edit", "Edit/update a cron job (alias: update)", {
    "type": "object",
    "properties": {
        "jobId": {"type": "string", "description": "Job ID"},
        "name": {"type": "string", "description": "Job name (alternative to jobId)"},
        "patch": {"type": "object", "description": "Fields to update (forwarded to OpenClaw)"},
        "tz": {"type": "string", "description": "New timezone ('local' or IANA)"},
        "cron": {"type": "string", "description": "New cron expression"},
        "systemEvent": {"type": "string", "description": "New system event text"},
        "message": {"type": "string", "description": "New agent message"},
        "at": {"type": "string", "description": "New one-shot time"},
        "every": {"type": "string", "description": "New interval"},
        "deleteAfterRun": {"type": "boolean"},
        "announce": {"type": "boolean"},
        "session": {"type": "string", "enum": ["main", "isolated"]},
        "wake": {"type": "string", "enum": ["now", "next-heartbeat"]},
    },
})
def tool_edit(params):
    job_id = params.get("jobId")
    name = params.get("name")
    tz = params.get("tz")

    # If setting tz to local, start tracking
    if tz == "local":
        state = load_json(STATE_FILE, {})
        update_location(state)
        save_json(STATE_FILE, state)
        resolved = get_current_tz(state)

        # Find the openclaw job id if we have a name
        if not job_id and name:
            result = openclaw_cron("list")
            for j in result.get("jobs", []):
                if j.get("name") == name:
                    job_id = j["id"]
                    break

        if job_id:
            # Update openclaw with resolved tz
            openclaw_cron("edit", job_id, "--tz", resolved)

            # Track in locron.json
            locron_jobs = load_json(LOCRON_FILE, [])
            locron_jobs = [j for j in locron_jobs if j.get("name") != name]

            # Get expr from openclaw
            result = openclaw_cron("list")
            expr = ""
            for j in result.get("jobs", []):
                if j.get("id") == job_id:
                    expr = j.get("schedule", {}).get("expr", "")
                    if not name:
                        name = j.get("name", "")
                    break

            locron_jobs.append({
                "name": name,
                "kind": "local_tz",
                "openclaw_id": job_id,
                "expr": expr,
                "current_tz": resolved,
            })
            save_json(LOCRON_FILE, locron_jobs)
            return {"ok": True, "tz": "local", "resolved": resolved}

    # Regular update — resolve ID and forward to openclaw
    jid = _resolve_job_id(name, job_id)
    if not jid:
        return {"error": f"Job not found: {name or job_id}"}

    args = ["edit", jid]

    # Map params to CLI flags
    flag_map = {
        "tz": "--tz", "cron": "--cron", "at": "--at", "every": "--every",
        "systemEvent": "--system-event", "message": "--message",
        "session": "--session", "wake": "--wake",
    }
    for key, flag in flag_map.items():
        val = params.get(key)
        if val is not None:
            args += [flag, str(val)]

    if params.get("deleteAfterRun"):
        args.append("--delete-after-run")
    if params.get("announce"):
        args.append("--announce")

    # Also handle patch object for backward compat with native cron tool
    patch = params.get("patch", {})
    if "enabled" in patch:
        args.append("--enable" if patch["enabled"] else "--disable")
    if "schedule" in patch:
        sched = patch["schedule"]
        if sched.get("expr"):
            args += ["--cron", sched["expr"]]
        if sched.get("tz"):
            if sched["tz"] == "local":
                # Start tracking this job
                return tool_edit({**params, "tz": "local", "patch": {}})
            args += ["--tz", sched["tz"]]

    # Update locron tracking if this is a tracked job
    locron_jobs = load_json(LOCRON_FILE, [])
    for lj in locron_jobs:
        if lj.get("openclaw_id") == jid and lj.get("kind") == "local_tz":
            if params.get("cron"):
                lj["expr"] = params["cron"]
            break
    save_json(LOCRON_FILE, locron_jobs)

    return openclaw_cron(*args)


@tool("enable", "Enable a cron job", {
    "type": "object",
    "properties": {"name": {"type": "string"}, "jobId": {"type": "string"}},
})
def tool_enable(params):
    jid = _resolve_job_id(params.get("name"), params.get("jobId"))
    if not jid:
        return {"error": f"Job not found: {params.get('name') or params.get('jobId')}"}
    _set_locron_enabled(params.get("name"), True)
    return openclaw_cron("enable", jid)


@tool("disable", "Disable a cron job", {
    "type": "object",
    "properties": {"name": {"type": "string"}, "jobId": {"type": "string"}},
})
def tool_disable(params):
    jid = _resolve_job_id(params.get("name"), params.get("jobId"))
    if not jid:
        return {"error": f"Job not found: {params.get('name') or params.get('jobId')}"}
    _set_locron_enabled(params.get("name"), False)
    return openclaw_cron("disable", jid)


@tool("run", "Trigger a cron job immediately", {
    "type": "object",
    "properties": {"jobId": {"type": "string"}, "name": {"type": "string"}},
})
def tool_run(params):
    jid = _resolve_job_id(params.get("name"), params.get("jobId"))
    if not jid:
        return {"error": f"Job not found: {params.get('name') or params.get('jobId')}"}
    return openclaw_cron("run", jid)


@tool("update", "Update a cron job (alias for edit)", {
    "type": "object",
    "properties": {
        "jobId": {"type": "string"}, "name": {"type": "string"},
        "patch": {"type": "object"}, "tz": {"type": "string"},
        "cron": {"type": "string"}, "systemEvent": {"type": "string"},
        "message": {"type": "string"}, "at": {"type": "string"},
        "every": {"type": "string"}, "deleteAfterRun": {"type": "boolean"},
        "announce": {"type": "boolean"},
        "session": {"type": "string", "enum": ["main", "isolated"]},
    },
})
def tool_update(params):
    return tool_edit(params)


@tool("runs", "Show run history for a cron job", {
    "type": "object",
    "properties": {
        "jobId": {"type": "string", "description": "Job ID"},
        "name": {"type": "string", "description": "Job name"},
    },
})
def tool_runs(params):
    jid = _resolve_job_id(params.get("name"), params.get("jobId"))
    if not jid:
        return {"error": f"Job not found: {params.get('name') or params.get('jobId')}"}
    return openclaw_cron("runs", jid)


@tool("wake", "Send a wake event", {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Wake event text"},
        "mode": {"type": "string", "enum": ["now", "next-heartbeat"]},
    },
    "required": ["text"],
})
def tool_wake(params):
    args = ["add", "--at", "+0s", "--delete-after-run",
            "--system-event", params["text"]]
    if params.get("mode") == "next-heartbeat":
        args += ["--wake", "next-heartbeat"]
    return openclaw_cron(*args)


@tool("status", "Show locron status: current location, timezone, tracked jobs", {
    "type": "object", "properties": {},
})
def tool_status(params):
    state = load_json(STATE_FILE, {})
    update_location(state)
    save_json(STATE_FILE, state)

    loc = state.get("_location")
    tz = get_current_tz(state)

    locron_jobs = load_json(LOCRON_FILE, [])
    local_tz = [j for j in locron_jobs if j.get("kind") == "local_tz"]
    geo = [j for j in locron_jobs if j.get("kind") == "geo"]

    return {
        "location": {"lat": loc["lat"], "lon": loc["lon"]} if loc else None,
        "timezone": tz,
        "local_time": datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "tracked_jobs": {"local_tz": len(local_tz), "geo": len(geo)},
        "local_tz_jobs": [{"name": j["name"], "expr": j.get("expr"),
                           "current_tz": j.get("current_tz")} for j in local_tz],
        "geo_jobs": [{"name": j["name"], "location": j.get("trigger", {}).get("location"),
                      "on": j.get("trigger", {}).get("on"), "enabled": j.get("enabled", True)}
                     for j in geo],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _params_to_cli_args(params):
    """Convert tool params to openclaw cron add CLI args."""
    args = []
    mapping = {
        "name": "--name",
        "cron": "--cron",
        "tz": "--tz",
        "at": "--at",
        "every": "--every",
        "systemEvent": "--system-event",
        "message": "--message",
        "session": "--session",
        "wake": "--wake",
    }
    for key, flag in mapping.items():
        val = params.get(key)
        if val is not None:
            args += [flag, str(val)]

    if params.get("deleteAfterRun"):
        args.append("--delete-after-run")
    if params.get("announce"):
        args.append("--announce")

    return args


def _resolve_job_id(name=None, job_id=None):
    """Resolve a job ID from name or direct id."""
    if job_id:
        return job_id
    if name:
        # Check locron tracking first
        locron_jobs = load_json(LOCRON_FILE, [])
        for j in locron_jobs:
            if j.get("name") == name and j.get("openclaw_id"):
                return j["openclaw_id"]
        # Fall back to openclaw cron list
        result = openclaw_cron("list")
        for j in result.get("jobs", []):
            if j.get("name") == name:
                return j.get("id")
    return None


def _set_locron_enabled(name, enabled):
    if not name:
        return
    locron_jobs = load_json(LOCRON_FILE, [])
    for j in locron_jobs:
        if j.get("name") == name:
            j["enabled"] = enabled
            save_json(LOCRON_FILE, locron_jobs)
            return


# ---------------------------------------------------------------------------
# MCP protocol handler (JSON-RPC 2.0 over stdio)
# ---------------------------------------------------------------------------

def handle_request(msg):
    method = msg.get("method", "")
    params = msg.get("params", {})
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "locron", "version": "0.1.0"},
            },
        }

    elif method == "notifications/initialized":
        return None  # no response needed

    elif method == "tools/list":
        tools_list = []
        for name, t in TOOLS.items():
            tools_list.append({
                "name": name,
                "description": t["description"],
                "inputSchema": t["schema"],
            })
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": tools_list},
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        if tool_name in TOOLS:
            try:
                result = TOOLS[tool_name]["fn"](tool_args)
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                    },
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {e}"}],
                        "isError": True,
                    },
                }
        else:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}

    else:
        # Unknown method
        if msg_id is not None:
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }
        return None  # notification, no response


def main():
    """MCP server: read JSON-RPC from stdin, write to stdout."""
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr,
                        format="%(asctime)s locron %(levelname)s %(message)s")
    log.info("locron MCP server starting")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_request(msg)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
