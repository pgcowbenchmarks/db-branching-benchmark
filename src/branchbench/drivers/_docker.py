"""Thin Docker helpers for container-based strategies (colima/docker)."""
from __future__ import annotations

import socket
import subprocess
import time


def _run(args: list[str], check=True, capture=True) -> str:
    r = subprocess.run(args, check=check, capture_output=capture, text=True)
    return (r.stdout or "").strip()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def exists(name: str) -> bool:
    out = _run(["docker", "ps", "-aq", "-f", f"name=^{name}$"], check=False)
    return bool(out)


def stop_rm(name: str) -> None:
    # -v removes the container's anonymous volumes too; without it, every
    # ephemeral/clone container leaks a multi-GB data volume and the VM disk
    # fills up (breaking later strategies with "table is full").
    subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, text=True)


def exec_(name: str, cmd: list[str], check=True) -> str:
    return _run(["docker", "exec", name] + cmd, check=check)


def container_disk_bytes(name: str, path: str = "/var/lib/postgresql/data") -> int | None:
    """Bytes on disk under `path` inside the container (du -sb)."""
    try:
        out = exec_(name, ["du", "-sb", path], check=True)
        return int(out.split()[0])
    except Exception:
        return None


def run_postgres(name: str, port: int, image: str, password: str,
                 db: str = "postgres", user: str = "postgres",
                 ready_timeout: float = 60.0, volumes: list[tuple[str, str]] | None = None) -> None:
    """Start a Postgres container and block until it accepts connections.

    volumes: list of (host_path, container_path) bind mounts.
    """
    stop_rm(name)
    args = ["docker", "run", "-d", "--name", name,
            "-e", f"POSTGRES_PASSWORD={password}",
            "-e", f"POSTGRES_USER={user}",
            "-e", f"POSTGRES_DB={db}",
            "-p", f"{port}:5432"]
    for host_path, cont_path in (volumes or []):
        args += ["-v", f"{host_path}:{cont_path}"]
    args += [
        # fsync off is a fair, consistent tuning applied to ALL pg strategies
        # (benchmark measures provisioning mechanism, not durability).
        image, "-c", "fsync=off", "-c", "synchronous_commit=off",
        "-c", "full_page_writes=off",
    ]
    _run(args)
    _wait_pg_ready(name, user, db, ready_timeout)


def _wait_pg_ready(name: str, user: str, db: str, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["docker", "exec", name, "pg_isready", "-U", user, "-d", db],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
        time.sleep(0.15)
    raise TimeoutError(f"postgres container {name} not ready in {timeout}s")
