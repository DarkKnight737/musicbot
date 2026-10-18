"""Small shared helpers: ffmpeg discovery, subprocess, temp dirs."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("utils")


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """Locate ffmpeg.exe, including winget's package folder (result is cached)."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    la = os.environ.get("LOCALAPPDATA", "")
    for hint in [
        Path(la) / "Microsoft" / "WinGet" / "Packages",
        Path(la) / "Microsoft" / "WinGet" / "Links",
    ]:
        if not hint.is_dir():
            continue
        for exe in hint.glob("**/ffmpeg.exe"):
            return str(exe)
    return None


def find_ffmpeg_dir() -> str | None:
    ff = find_ffmpeg()
    return str(Path(ff).parent) if ff else None


async def run_cmd(cmd: list[str], timeout: float = 120) -> str:
    """Run a process, return stderr/stdout text; raise on failure."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            # Reap so a killed child doesn't linger and leak pipe handles.
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        raise RuntimeError(f"command timed out: {cmd[0]}")
    text = (err or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        tail = text[-400:]
        raise RuntimeError(f"{Path(cmd[0]).name} exited {proc.returncode}: {tail}")
    return text


def run_sync(cmd: list[str], timeout: float = 120) -> str:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} exited {proc.returncode}: {proc.stderr[-400:]}")
    return proc.stdout + proc.stderr


def new_task_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="mfind_"))


def fmt_seconds(s: float | int | None) -> str:
    if not s or s <= 0:
        return "?"
    s = int(s)
    return f"{s // 60}:{s % 60:02d}"
