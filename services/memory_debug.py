"""Process memory (RSS) reporting for the 512 MB Render instance.

- `rss_mb()` is cheap and always available (used by /health).
- `log_rss(tag)` logs the current RSS; a no-op unless MEMORY_DEBUG_LOG=1. Call it around cache rebuilds.
- `start_periodic_logging()` starts an asyncio task that logs RSS every 60 s when MEMORY_DEBUG_LOG=1 (default off).

No third-party dependency: Linux reads /proc/self/status (VmRSS), Windows uses GetProcessMemoryInfo via ctypes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger("memory_debug")

INTERVAL_SECONDS = 60


def enabled() -> bool:
    return os.environ.get("MEMORY_DEBUG_LOG", "").strip().lower() in {"1", "true", "yes", "on"}


def _rss_bytes_linux() -> int | None:
    try:
        with open("/proc/self/status", "rb") as handle:
            for line in handle:
                if line.startswith(b"VmRSS:"):
                    return int(line.split()[1]) * 1024  # value is in kB
    except (OSError, ValueError, IndexError):
        return None
    return None


def _rss_bytes_windows() -> int | None:
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_current_process.restype = wintypes.HANDLE
        get_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD]
        get_memory_info.restype = wintypes.BOOL
        if get_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
            return int(counters.WorkingSetSize)
    except Exception:  # noqa: BLE001
        return None
    return None


def rss_mb() -> float | None:
    raw = _rss_bytes_windows() if sys.platform == "win32" else _rss_bytes_linux()
    return round(raw / (1024 * 1024), 1) if raw is not None else None


def log_rss(tag: str) -> None:
    if enabled():
        logger.info("[MEMORY] %s rss_mb=%s", tag, rss_mb())


async def _loop(interval: float) -> None:
    while True:
        log_rss("periodic")
        await asyncio.sleep(interval)


def start_periodic_logging(interval: float = INTERVAL_SECONDS) -> asyncio.Task | None:
    """Start the 60 s RSS logger on the running loop; returns None (nothing started) unless MEMORY_DEBUG_LOG=1."""
    if not enabled():
        return None
    logger.info("[MEMORY] MEMORY_DEBUG_LOG on: logging RSS every %ss", interval)
    return asyncio.get_running_loop().create_task(_loop(interval), name="memory-debug-log")
