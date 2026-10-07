"""API and worker processes this tool starts, and the Postgres restart helpers.

Killing a process uses SIGKILL so a mid-transaction worker rolls back in Postgres.
Polls fail the caller when the condition is not met. They do not assume success.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import IO

import httpx
from sqlalchemy.engine.url import make_url


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until(predicate, timeout: float, interval: float = 0.05) -> bool:
    """Return True when predicate becomes true. Return False at the deadline."""

    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def wait_http_ok(base_url: str, path: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    client = httpx.Client(base_url=base_url, timeout=2.0)
    try:
        while time.monotonic() < deadline:
            try:
                response = client.get(path)
            except httpx.HTTPError:
                time.sleep(0.05)
                continue
            if response.status_code == 200:
                return True
            time.sleep(0.05)
    finally:
        client.close()
    return False


class ProcessSet:
    """Child processes owned by one local run."""

    def __init__(self, *, env: dict[str, str], log_dir: Path) -> None:
        self.env = env
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.api: subprocess.Popen[bytes] | None = None
        self.api_port: int | None = None
        self.workers: list[tuple[str, subprocess.Popen[bytes]]] = []
        self._logs: list[IO[bytes]] = []

    @property
    def base_url(self) -> str:
        if self.api_port is None:
            raise RuntimeError("API port is not set")
        return f"http://127.0.0.1:{self.api_port}"

    def start_api(self, port: int | None = None) -> str:
        if self.api is not None and self.api.poll() is None:
            raise RuntimeError("API is already running")
        chosen = port if port is not None else free_port()
        log = self._open_log("api.log")
        self.api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "simcore.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(chosen),
                "--log-level",
                "warning",
            ],
            env=self.env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.api_port = chosen
        if not wait_http_ok(self.base_url, "/health/ready", timeout=30):
            tail = self._tail("api.log")
            raise RuntimeError(f"API did not become ready on {self.base_url}. log: {tail}")
        return self.base_url

    def kill_api(self) -> None:
        if self.api is None:
            return
        _kill(self.api)
        self.api = None

    def restart_api(self) -> str:
        port = self.api_port
        self.kill_api()
        # The port is free once the process is gone. Reuse it so clients keep the same origin.
        return self.start_api(port)

    def start_worker(self, name: str) -> subprocess.Popen[bytes]:
        env = dict(self.env)
        env["SIMCORE_WORKER_ID"] = name[:80]
        log = self._open_log(f"worker-{name}.log")
        proc = subprocess.Popen(
            [sys.executable, "-m", "simcore.worker"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.workers.append((name, proc))
        return proc

    def stop_workers(self, *, kill: bool) -> None:
        for _name, proc in self.workers:
            if kill:
                _kill(proc)
            else:
                _terminate(proc)
        self.workers.clear()

    def kill_worker(self, proc: subprocess.Popen[bytes]) -> None:
        _kill(proc)
        self.workers = [(name, item) for name, item in self.workers if item.pid != proc.pid]

    def worker_pids(self) -> list[int]:
        alive = []
        for _name, proc in self.workers:
            if proc.poll() is None:
                alive.append(proc.pid)
        return alive

    def api_pid(self) -> int | None:
        if self.api is None or self.api.poll() is not None:
            return None
        return self.api.pid

    def close(self) -> None:
        self.stop_workers(kill=True)
        self.kill_api()
        for handle in self._logs:
            try:
                handle.close()
            except Exception:
                pass
        self._logs.clear()

    def _open_log(self, name: str) -> IO[bytes]:
        handle = (self.log_dir / name).open("ab")
        self._logs.append(handle)
        return handle

    def _tail(self, name: str) -> str:
        path = self.log_dir / name
        if not path.is_file():
            return ""
        data = path.read_bytes()[-1500:]
        return data.decode("utf-8", errors="replace")


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill(proc)


def child_env(database_url: str, admin_token: str) -> dict[str, str]:
    """Environment for the API and worker this tool starts.

    The command rate limit is raised so the run measures command handling.
    Production stays at its own limit. This value is not written to a file.
    """

    env = os.environ.copy()
    env.update(
        {
            "SIMCORE_DATABASE_URL": database_url,
            "SIMCORE_ADMIN_TOKEN": admin_token,
            "SIMCORE_ENV": "development",
            "SIMCORE_EMBEDDED_WORKER": "false",
            "SIMCORE_MONITOR_API_SAMPLER": "false",
            "SIMCORE_MONITOR_SAMPLE_SECONDS": "0",
            "SIMCORE_WORKER_POLL_SECONDS": "0.05",
            "SIMCORE_COMMAND_RATE_LIMIT": "1000",
            "SIMCORE_COMMAND_RATE_WINDOW_SECONDS": "60",
            "SIMCORE_PLAYER_LOGIN_MAX_FAILURES": "100000",
            "SIMCORE_PLAYER_LOGIN_IP_MAX_FAILURES": "100000",
        }
    )
    return env


def terminate_backends(database_url: str) -> int:
    """Drop other client backends on this database. Returns how many were signaled."""

    import psycopg

    url = make_url(database_url)
    conn = psycopg.connect(
        host=url.host or "127.0.0.1",
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        dbname=url.database,
        autocommit=True,
        connect_timeout=5,
    )
    try:
        rows = conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND backend_type = 'client backend'
            """
        ).fetchall()
        return len(rows)
    finally:
        conn.close()


def postgres_is_up(database_url: str) -> bool:
    import psycopg

    url = make_url(database_url)
    try:
        conn = psycopg.connect(
            host=url.host or "127.0.0.1",
            port=url.port or 5432,
            user=url.username,
            password=url.password,
            dbname=url.database,
            connect_timeout=2,
        )
    except Exception:
        return False
    try:
        conn.execute("SELECT 1")
        return True
    except Exception:
        return False
    finally:
        conn.close()


def restart_postgres(database_url: str) -> tuple[bool, str]:
    """Restart the local Postgres serving this URL.

    Returns (ok, method). ok is False when no restart method is available.
    The caller reports that as INCOMPLETE rather than inventing a restart.
    """

    custom = os.environ.get("SIMCORE_LOAD_PG_RESTART_CMD", "").strip()
    if custom:
        completed = subprocess.run(custom, shell=True, timeout=120, check=False)
        return completed.returncode == 0, "SIMCORE_LOAD_PG_RESTART_CMD"

    container = _docker_postgres_container()
    if container:
        completed = subprocess.run(["docker", "restart", container], timeout=120, check=False)
        return completed.returncode == 0, f"docker restart {container}"

    cluster = _pg_ctlcluster_restart()
    if cluster is not None:
        return cluster
    if shutil.which("systemctl"):
        completed = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "postgresql"],
            timeout=120,
            check=False,
        )
        if completed.returncode == 0:
            return True, "systemctl restart postgresql"
    return False, "no postgres restart method was available"


def _docker_postgres_container() -> str | None:
    if shutil.which("docker") is None:
        return None
    completed = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}}\t{{.Ports}}"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and ":5432->" in parts[1]:
            return parts[0].strip()
    return None


def _pg_ctlcluster_restart() -> tuple[bool, str] | None:
    if shutil.which("pg_lsclusters") is None or shutil.which("pg_ctlcluster") is None:
        return None
    completed = subprocess.run(
        ["pg_lsclusters", "--no-header"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        return None
    target: tuple[str, str] | None = None
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        version, name, port, status = fields[0], fields[1], fields[2], fields[3]
        if port == "5432" and status == "online":
            target = (version, name)
            break
    if target is None:
        return None
    version, name = target
    restarted = subprocess.run(
        ["sudo", "-n", "pg_ctlcluster", version, name, "restart"],
        timeout=120,
        check=False,
    )
    return restarted.returncode == 0, f"pg_ctlcluster {version} {name} restart"
