"""Launch and track one detached WhatsApp worker per connection on Windows."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from django.conf import settings
from ..models import WhatsAppConnection


WORKER_ROOT = Path(settings.BASE_DIR) / "runtime" / "whatsapp_workers"
WORKER_HEARTBEAT_SECONDS = 15


def _pid_file(connection_id: int) -> Path:
    return WORKER_ROOT / f"connection_{connection_id}.pid"


def _heartbeat_file(connection_id: int) -> Path:
    return WORKER_ROOT / f"connection_{connection_id}.heartbeat"


def heartbeat_worker(connection_id: int) -> None:
    """Record that the bot loop is alive, independently from the web server."""
    WORKER_ROOT.mkdir(parents=True, exist_ok=True)
    _heartbeat_file(connection_id).touch()


def worker_is_healthy(connection_id: int, pid: int) -> bool:
    """A PID alone is not reliable: Windows can reuse it after a bot crashes."""
    heartbeat = _heartbeat_file(connection_id)
    try:
        heartbeat_is_fresh = time.time() - heartbeat.stat().st_mtime < WORKER_HEARTBEAT_SECONDS
    except FileNotFoundError:
        heartbeat_is_fresh = False
    return heartbeat_is_fresh and _pid_is_running(pid)


def finish_worker(connection_id: int, pid: int) -> None:
    """Remove only this worker's markers; never remove a newer worker's PID."""
    pid_file = _pid_file(connection_id)
    try:
        recorded_pid = int(pid_file.read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError):
        recorded_pid = 0
    if recorded_pid == pid:
        pid_file.unlink(missing_ok=True)
        _heartbeat_file(connection_id).unlink(missing_ok=True)


def _pid_is_running(pid: int) -> bool:
    """Return whether a PID still exists, without assuming a Unix platform."""
    if pid <= 0:
        return False
    try:
        # On Windows this raises PermissionError for an existing process owned by
        # another security context, which still means the PID is alive.
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def start_background_worker(connection_id: int, *, restart: bool = False) -> bool:
    """Start a QR worker, optionally replacing the current healthy one."""
    WORKER_ROOT.mkdir(parents=True, exist_ok=True)
    pid_file = _pid_file(connection_id)
    try:
        previous_pid = int(pid_file.read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError):
        previous_pid = 0

    if worker_is_healthy(connection_id, previous_pid):
        if not restart:
            return False
        # This PID has a fresh heartbeat from our worker, so it is safe to stop
        # before reopening the same Chromium profile. Closing Chromium does not
        # unlink the WhatsApp device; its persistent profile is kept on disk.
        try:
            os.kill(previous_pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 5
        while _pid_is_running(previous_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _pid_is_running(previous_pid):
            return False

    pid_file.unlink(missing_ok=True)
    _heartbeat_file(connection_id).unlink(missing_ok=True)
    log_file = WORKER_ROOT / f"connection_{connection_id}.log"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with log_file.open("ab") as log:
        environment = os.environ.copy()
        connection = WhatsAppConnection.objects.get(pk=connection_id)
        role = "client" if connection.kind == WhatsAppConnection.Kind.CLIENT else "guardian"
        client_id = getattr(getattr(connection, "saas_session", None), "client_id", None)
        client_id = client_id or (f"customer-{connection.owner_id}" if role == "client" else "guardian-master")
        environment.update({
            "WHATSAPP_BOT_CALLBACK_URL": settings.WHATSAPP_BOT_CALLBACK_URL,
            "WHATSAPP_BOT_CALLBACK_TOKEN": settings.WHATSAPP_BOT_CALLBACK_TOKEN,
            "WHATSAPP_BOT_RUNTIME_ROOT": str(Path(settings.BASE_DIR) / "runtime"),
            "WHATSAPP_MONITOR_CONNECTION_ID": str(connection_id if role == "monitor" else ""),
            "WHATSAPP_GUARDIAN_CONNECTION_ID": str(connection_id if role == "guardian" else ""),
            "WHATSAPP_ROLE": role,
            "WHATSAPP_CLIENT_ID": client_id,
            "WHATSAPP_AUTO_START": "1",
            "PORT": str(3000 + connection_id),
        })
        process = subprocess.Popen(
            ["node", "index.js"],
            cwd=Path(settings.BASE_DIR) / "whatsapp-service",
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            env=environment,
        )
    pid_file.write_text(str(process.pid), encoding="ascii")
    return True


def stop_background_worker(connection_id: int) -> bool:
    """Stop only the worker registered for this connection."""
    try:
        pid = int(_pid_file(connection_id).read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError):
        return False
    if _pid_is_running(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    finish_worker(connection_id, pid)
    return True
