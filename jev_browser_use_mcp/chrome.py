"""Headless Chrome we own: spawn it, point browser-harness at it, and never leak it.

browser-harness cannot launch a headless browser -- it only ever attaches to a
DevTools endpoint. So "headless mode" means this module spawns Chrome itself on
a free port with a throwaway profile and exports BU_CDP_URL. Attached mode
spawns nothing and touches neither the user's Chrome nor its daemon.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CACHE = Path.home() / ".cache" / "jev-browser-use-mcp"

# Order matters: the two env vars are the convention browser-harness already honors.
_MAC = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
_LINUX = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]


def find_chrome() -> str:
    """Absolute path to a Chrome binary, or raise with the fix named."""
    for var in ("BH_CHROME_PATH", "CHROME_PATH"):
        path = os.environ.get(var)
        if path and Path(path).is_file():
            return path
    if sys.platform == "darwin":
        for path in _MAC:
            if Path(path).is_file():
                return path
    for name in _LINUX:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "no Chrome found -- install Google Chrome, or set BH_CHROME_PATH to its binary. "
        "This server does not bundle a browser."
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("container") is not None


def _extra_flags() -> list[str]:
    """--no-sandbox is a real security downgrade; only in a container, or when asked."""
    explicit = os.environ.get("JEV_MCP_CHROME_FLAGS")
    if explicit:
        return explicit.split()
    if _in_container():
        return ["--no-sandbox", "--disable-dev-shm-usage"]
    return []


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def sweep_orphans() -> int:
    """Remove profile dirs whose owning server is gone, killing a Chrome still holding one.

    atexit does not run on SIGKILL, so this is how a hard kill gets cleaned up:
    the next start does it. Returns the number of directories removed.
    """
    removed = 0
    if not CACHE.is_dir():
        return 0
    for entry in CACHE.iterdir():
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        if _alive(int(entry.name)):
            continue
        try:
            chrome_pid = int((entry / "chrome.pid").read_text().strip())
        except (OSError, ValueError):
            chrome_pid = 0
        if chrome_pid and _alive(chrome_pid):
            try:
                os.kill(chrome_pid, signal.SIGTERM)
            except OSError:
                pass
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed


class HeadlessChrome:
    """A Chrome process and temp profile owned entirely by this server."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self.profile = CACHE / str(os.getpid())
        self.cdp_url: str | None = None

    def start(self, timeout_s: float = 30.0) -> str:
        """Spawn Chrome and return its DevTools base URL once it answers."""
        if self.cdp_url:
            return self.cdp_url
        binary = find_chrome()
        port = _free_port()
        self.profile.mkdir(parents=True, exist_ok=True)
        argv = [
            binary,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            *_extra_flags(),
        ]
        # Chrome's own chatter must never reach stdout: that is the MCP transport.
        self.proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.profile / "chrome.pid").write_text(str(self.proc.pid))

        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.close()
                raise RuntimeError(f"Chrome exited immediately (code {self.proc.returncode}); tried: {' '.join(argv)}")
            try:
                with urllib.request.urlopen(f"{url}/json/version", timeout=1) as response:
                    json.load(response)
                self.cdp_url = url
                return url
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                time.sleep(0.1)
        self.close()
        raise RuntimeError(f"Chrome did not open a DevTools port within {timeout_s:g}s")

    def close(self) -> None:
        """Kill the process and remove the profile. Safe to call twice."""
        proc, self.proc, self.cdp_url = self.proc, None, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)
