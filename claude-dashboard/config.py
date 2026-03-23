"""Configuration constants for Claude Dashboard."""

import os
import pathlib

PORT = int(os.environ.get("PORT", 3000))
SESSION_STATS_PATH = pathlib.Path(
    os.environ.get("SESSION_STATS_PATH", str(pathlib.Path.home() / ".claude" / ".session-stats.json"))
)
POLL_INTERVAL_S = 3.0
PUBLIC_DIR = pathlib.Path(__file__).parent / "public"
MAX_SESSIONS = 500
VALID_TOKEN_TYPES = frozenset({"input", "output", "cache_read", "cache_creation"})
BODY_SIZE_LIMIT = 1_048_576  # 1MB
DELETED_IDS_PATH = SESSION_STATS_PATH.parent / ".dashboard-deleted-sessions.json"
