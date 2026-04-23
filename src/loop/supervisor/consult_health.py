#!/usr/bin/env python3
"""Consult MCP server health check with caching."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

_MCP_INIT_REQUEST = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "healthcheck", "version": "0.1.0"},
        },
    }
)


def check_consult_health(
    binary_path: str,
    timeout_sec: int = 10,
    env: Optional[dict] = None,
) -> Tuple[bool, str]:
    """Probe the consult MCP server binary for startup health."""
    if not Path(binary_path).is_file():
        return False, f"binary not found: {binary_path}"

    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            [binary_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        stdout, stderr = proc.communicate(
            input=_MCP_INIT_REQUEST + "\n",
            timeout=timeout_sec,
        )
        if proc.returncode != 0:
            preview = (stderr or stdout or "")[:300].strip()
            return False, f"exit {proc.returncode}: {preview}"

        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "result" in obj:
                return True, ""

        preview = (stdout or "")[:300].strip()
        return False, f"no valid MCP response: {preview}"
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout_sec}s"
    except FileNotFoundError:
        return False, f"binary not found: {binary_path}"
    except Exception as exc:
        return False, str(exc)
    finally:
        if proc is not None:
            _cleanup_process(proc)


def _cleanup_process(proc: subprocess.Popen) -> None:
    """Terminate and reap a subprocess."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
    except OSError:
        pass


class ConsultHealthCache:
    """Caches consult health results to avoid redundant probes."""

    def __init__(self, cache_ttl_sec: int = 300) -> None:
        self._cache_ttl_sec = cache_ttl_sec
        self._healthy: Optional[bool] = None
        self._error: str = ""
        self._checked_at: float = 0.0
        self._cached_binary_path: str = ""

    def check(
        self,
        binary_path: str,
        timeout_sec: int = 10,
        env: Optional[dict] = None,
    ) -> Tuple[bool, str]:
        """Return cached result if fresh, otherwise probe and cache."""
        now = time.monotonic()
        if (
            self._healthy is not None
            and binary_path == self._cached_binary_path
            and (now - self._checked_at) < self._cache_ttl_sec
        ):
            return self._healthy, self._error

        healthy, error = check_consult_health(binary_path, timeout_sec, env)
        self._healthy = healthy
        self._error = error
        self._checked_at = now
        self._cached_binary_path = binary_path

        status = "healthy" if healthy else f"unhealthy: {error}"
        log.info("consult_health_check result=%s binary=%s", status, binary_path)
        return healthy, error

    def invalidate(self) -> None:
        self._healthy = None
