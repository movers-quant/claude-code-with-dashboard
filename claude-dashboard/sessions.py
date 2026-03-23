"""Session state management — merges stats file, process info, and OTLP data."""

import json
import time

from config import SESSION_STATS_PATH, MAX_SESSIONS, DELETED_IDS_PATH
from processes import get_claude_processes, get_working_dir_for_pid

# In-memory state
sessions: dict[str, dict] = {}
token_metrics: dict[str, dict] = {}


def _load_deleted_ids() -> set[str]:
    try:
        return set(json.loads(DELETED_IDS_PATH.read_text()))
    except Exception:
        return set()


def _save_deleted_ids() -> None:
    try:
        DELETED_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
        DELETED_IDS_PATH.write_text(json.dumps(sorted(_deleted_ids)))
    except Exception:
        pass


_deleted_ids: set[str] = _load_deleted_ids()


def read_session_stats() -> dict:
    """Read and parse ~/.claude/.session-stats.json."""
    try:
        raw = SESSION_STATS_PATH.read_text()
        return json.loads(raw)
    except Exception:
        return {"sessions": {}}


async def merge_session_data() -> list[dict]:
    """Combine all data sources into a unified session list."""
    stats = read_session_stats()
    procs = await get_claude_processes()
    now = time.time() * 1000  # ms

    # Update from stats file (skip manually deleted sessions)
    for sid, data in (stats.get("sessions") or {}).items():
        if sid in _deleted_ids:
            continue
        existing = sessions.get(sid, {})
        sessions[sid] = {
            **existing,
            "id": sid,
            "tool_counts": data.get("tool_counts", {}),
            "last_tool": data.get("last_tool"),
            "total_calls": data.get("total_calls", 0),
            "started_at": data["started_at"] * 1000 if data.get("started_at") else existing.get("started_at"),
            "updated_at": data["updated_at"] * 1000 if data.get("updated_at") else existing.get("updated_at"),
            "status": "unknown",
        }

    # Reset status
    for s in sessions.values():
        s["status"] = "stopped"
        s["pid"] = None
        s["cpu"] = None
        s["mem"] = None
        s["working_dir"] = None

    # Match processes to sessions by recency
    sorted_sessions = sorted(sessions.values(), key=lambda s: s.get("updated_at") or 0, reverse=True)
    assigned_pids: set[int] = set()

    for i in range(min(len(procs), len(sorted_sessions))):
        p = procs[i]
        sess = sorted_sessions[i]
        if p["pid"] not in assigned_pids:
            assigned_pids.add(p["pid"])
            sess["status"] = "running"
            sess["pid"] = p["pid"]
            sess["cpu"] = p["cpu"]
            sess["mem"] = p["mem"]
            sess["working_dir"] = get_working_dir_for_pid(p["pid"])

    # Create stub sessions for unmatched processes (e.g. new sessions with no tool calls yet)
    for p in procs:
        if p["pid"] not in assigned_pids:
            stub_id = f"pid-{p['pid']}"
            existing = sessions.get(stub_id)
            working_dir = get_working_dir_for_pid(p["pid"])
            sessions[stub_id] = {
                "id": stub_id,
                "status": "running",
                "pid": p["pid"],
                "cpu": p["cpu"],
                "mem": p["mem"],
                "working_dir": working_dir,
                "tool_counts": {},
                "total_calls": 0,
                "started_at": existing["started_at"] if existing else now,
                "updated_at": now,
            }

    # Remove stale pid-* stubs whose PID was matched to a real session
    for pid in assigned_pids:
        sessions.pop(f"pid-{pid}", None)

    # Mark recently-updated sessions as idle
    for s in sessions.values():
        if s.get("updated_at") and now - s["updated_at"] < 30000 and s["status"] == "stopped":
            s["status"] = "idle"

    # Merge OTLP token metrics
    for sid, metrics in token_metrics.items():
        if sid not in sessions:
            sessions[sid] = {"id": sid, "status": "unknown", "total_calls": 0, "tool_counts": {}}
        sessions[sid]["tokens"] = metrics

    # Evict stale sessions if over limit
    if len(sessions) > MAX_SESSIONS:
        by_age = sorted(sessions.items(), key=lambda kv: kv[1].get("updated_at") or 0)
        for sid, _ in by_age[: len(sessions) - MAX_SESSIONS]:
            del sessions[sid]

    return sorted(
        [s for s in sessions.values() if s.get("id")],
        key=lambda s: s.get("updated_at") or 0,
        reverse=True,
    )


def delete_session(session_id: str) -> bool:
    """Remove a session by ID. Returns True if it existed."""
    found = session_id in sessions or session_id in token_metrics
    sessions.pop(session_id, None)
    token_metrics.pop(session_id, None)
    if found:
        _deleted_ids.add(session_id)
        _save_deleted_ids()
    return found
