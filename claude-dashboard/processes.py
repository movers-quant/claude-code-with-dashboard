"""Claude process detection via async subprocess."""

import asyncio
import os
from typing import Optional


async def get_claude_processes() -> list[dict]:
    """Detect running claude CLI processes via ``ps aux`` (async)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "aux", "--no-headers",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        lines = stdout.decode(errors="replace").splitlines()
    except Exception:
        return []

    result: list[dict] = []
    for line in lines:
        if "claude" not in line or "claude-dashboard" in line or "grep" in line:
            continue
        parts = line.split()
        if len(parts) < 11:
            continue
        exe = parts[10]
        if exe == "claude" or exe.endswith("/claude"):
            result.append({
                "pid": int(parts[1]),
                "cpu": parts[2],
                "mem": parts[3],
                "cmd": " ".join(parts[10:]),
            })
    return result


def get_working_dir_for_pid(pid: int) -> Optional[str]:
    """Read working directory from /proc (Linux only)."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None
