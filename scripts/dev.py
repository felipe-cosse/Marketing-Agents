#!/usr/bin/env python3
"""Supervise the complete native local platform using already installed tools."""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[1]
NODE_VERSION = "v24.20.0"
PNPM_VERSION = "11.24.0"


class NativeStartupError(RuntimeError):
    """Safe prerequisite, readiness, or supervised-child failure."""


@dataclass(frozen=True)
class Tools:
    python: Path
    node: Path
    pnpm: Path


def _version(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        raise NativeStartupError("native_tool_unavailable: run make bootstrap first") from None
    return result.stdout.strip()


def prerequisites(root: Path = ROOT, *, node: Path | None = None) -> Tools:
    python = root / ".venv" / "bin" / "python"
    if not python.is_file() or not (root / "apps/web/node_modules/.bin/vite").is_file():
        raise NativeStartupError("native_dependencies_missing: run make bootstrap first")
    if not _version([str(python), "--version"]).startswith("Python 3.12."):
        raise NativeStartupError("native_python_version: Python 3.12 is required")
    selected_node = node or Path(shutil.which("node") or "/missing/node")
    if _version([str(selected_node), "--version"]) != NODE_VERSION:
        raise NativeStartupError(f"native_node_version: activate Node {NODE_VERSION[1:]}")
    pnpm = Path(shutil.which("pnpm") or "/missing/pnpm")
    if _version([str(pnpm), "--version"]) != PNPM_VERSION:
        raise NativeStartupError(f"native_pnpm_version: pnpm {PNPM_VERSION} is required")
    return Tools(python, selected_node, pnpm)


def state_directory(path: Path, *, root: Path = ROOT) -> Path:
    candidate = path.absolute()
    if any(part.is_symlink() for part in (candidate, *candidate.parents)):
        raise NativeStartupError("native_state_path: symlink state directories are unsupported")
    resolved = candidate.resolve()
    if resolved in {Path("/"), Path.home(), root.resolve(), Path("/tmp").resolve()}:
        raise NativeStartupError("native_state_path: choose a dedicated task directory")
    if len(resolved.parts) < 3:
        raise NativeStartupError("native_state_path: choose a dedicated task directory")
    return resolved


def safe_environment(
    state: Path,
    tools: Tools,
    *,
    api_port: int,
    web_port: int,
    inherited: Mapping[str, str] | None = None,
    root: Path = ROOT,
) -> dict[str, str]:
    inherited = os.environ if inherited is None else inherited
    environment = {
        name: inherited[name]
        for name in ("HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")
        if name in inherited
    }
    environment.update(
        {
            "PATH": str(tools.node.parent) + os.pathsep + inherited.get("PATH", os.defpath),
            "PYTHONUNBUFFERED": "1",
            "APP_ENV": "local",
            "AUTH_MODE": "local",
            "LLM_PROVIDER": "mock",
            "CONNECTOR_MODE": "mock",
            "ALLOW_EXTERNAL_NETWORK": "false",
            "REAL_LLM_OPT_IN": "false",
            "REAL_CONNECTOR_OPT_IN": "false",
            "API_HOST": "127.0.0.1",
            "API_PORT": str(api_port),
            "API_TRUSTED_ORIGINS": json.dumps(
                [
                    f"http://127.0.0.1:{api_port}",
                    f"http://localhost:{api_port}",
                    f"http://127.0.0.1:{web_port}",
                    f"http://localhost:{web_port}",
                ]
            ),
            "MARKETING_AGENTS_NATIVE_API_PORT": str(api_port),
            "DATABASE_URL": f"sqlite+aiosqlite:///{state / 'marketing_agents.db'}",
            "MARKETING_AGENTS_DIGEST_KEY_PATH": str(state / "secrets" / "digest.key"),
            "CATALOG_ROOT": str(root / "catalog" / "v1"),
        }
    )
    return environment


def require_free_ports(api_port: int, web_port: int) -> None:
    if api_port == web_port or not all(1024 <= port <= 65535 for port in (api_port, web_port)):
        raise NativeStartupError("native_ports_invalid: use distinct ports from 1024 through 65535")
    for port in (api_port, web_port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # Match the servers' restart behavior: closed connections in TIME_WAIT
            # do not mean another live listener owns this port.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise NativeStartupError(f"native_port_in_use: loopback port {port}") from None


@contextmanager
def installation_lock(state: Path) -> Iterator[None]:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = state.stat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise NativeStartupError(
            "native_state_permissions: state must be owned by the current user with mode 0700"
        )
    path = state / "native.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NativeStartupError(
                "native_already_running: this installation is supervised"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@dataclass
class Child:
    name: str
    process: subprocess.Popen[bytes]
    log: BinaryIO
    initialized: bool = False


class Supervisor:
    def __init__(self, *, environment: dict[str, str], logs: Path, grace: float = 25) -> None:
        self.environment = environment
        self.logs = logs
        self.grace = grace
        self.children: list[Child] = []
        self.stopping = False

    def start(self, name: str, command: Sequence[str], *, cwd: Path) -> Child:
        descriptor = os.open(self.logs / f"{name}.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        log = os.fdopen(descriptor, "wb")
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException:
            log.close()
            raise
        child = Child(name, process, log)
        self.children.append(child)
        return child

    def initialize(self, name: str, command: Sequence[str], *, cwd: Path) -> None:
        child = self.start(name, command, cwd=cwd)
        deadline = time.monotonic() + 90
        while child.process.poll() is None:
            if self.stopping or time.monotonic() >= deadline:
                raise NativeStartupError(f"native_initialization_interrupted: {name}")
            time.sleep(0.1)
        if child.process.returncode != 0:
            raise NativeStartupError(f"native_initialization_failed: {name}; inspect local logs")
        child.initialized = True

    def check_children(self, active: Sequence[Child]) -> None:
        failed = next((child for child in active if child.process.poll() is not None), None)
        if failed is not None:
            raise NativeStartupError(f"native_child_exited: {failed.name}; inspect local logs")

    def close(self) -> None:
        # Every process gets its own new session. Only those exact groups are stopped.
        alive = [child for child in self.children if not child.initialized]
        for child in alive:
            with suppress(ProcessLookupError):
                os.killpg(child.process.pid, signal.SIGTERM)
        deadline = time.monotonic() + self.grace
        while time.monotonic() < deadline:
            remaining = False
            for child in alive:
                child.process.poll()
                try:
                    os.killpg(child.process.pid, 0)
                    remaining = True
                except ProcessLookupError:
                    continue
            if not remaining:
                break
            time.sleep(0.05)
        for child in alive:
            with suppress(ProcessLookupError):
                os.killpg(child.process.pid, signal.SIGKILL)
            child.process.wait(timeout=5)
        for child in self.children:
            child.log.close()


def _get(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read(65_537)
        if len(body) > 65_536:
            raise NativeStartupError("native_readiness_response_too_large")
        return response.status, body
    finally:
        connection.close()


def ready(api_port: int, web_port: int) -> bool:
    try:
        api_status, api_body = _get(api_port, "/health/ready")
        web_status, _ = _get(web_port, "/")
        session_status, session_body = _get(web_port, "/api/v1/session")
        session = json.loads(session_body)
        return (
            api_status == web_status == session_status == 200
            and json.loads(api_body).get("status") == "ready"
            and session.get("authMode") == "local"
            and session.get("modelMode") == "mock"
            and session.get("connectorMode") == "mock"
            and session.get("networkPermission") is False
        )
    except (OSError, ValueError, http.client.HTTPException):
        return False


def run(args: argparse.Namespace) -> None:
    tools = prerequisites(node=args.node)
    state = state_directory(args.state_dir)
    require_free_ports(args.api_port, args.web_port)
    environment = safe_environment(state, tools, api_port=args.api_port, web_port=args.web_port)
    with installation_lock(state):
        logs = Path(tempfile.mkdtemp(prefix="native-run-", dir=state))
        supervisor = Supervisor(environment=environment, logs=logs)
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

        def stop(signum: int, frame: object) -> None:
            del signum, frame
            supervisor.stopping = True

        for sig in previous:
            signal.signal(sig, stop)
        try:
            python = str(tools.python)
            supervisor.initialize(
                "local-secret",
                [
                    python,
                    "-m",
                    "marketing_agents.workers.local_secret_init",
                    "--database-url",
                    environment["DATABASE_URL"],
                    "--key-path",
                    environment["MARKETING_AGENTS_DIGEST_KEY_PATH"],
                ],
                cwd=state,
            )
            for command in ("migrate", "seed"):
                supervisor.initialize(
                    command,
                    [python, "-m", "marketing_agents.workers.database_cli", command],
                    cwd=state,
                )
            active = [
                supervisor.start(
                    "api", [python, "-m", "marketing_agents.workers.serve_api"], cwd=state
                )
            ]
            for kind in ("run", "scheduler"):
                active.append(
                    supervisor.start(
                        f"{kind}-worker",
                        [
                            python,
                            "-m",
                            f"marketing_agents.workers.{kind}_worker",
                            "--health-file",
                            str(logs / f"{kind}-worker.health.json"),
                        ],
                        cwd=state,
                    )
                )
            active.append(
                supervisor.start(
                    "web",
                    [
                        str(tools.pnpm),
                        "exec",
                        "vite",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(args.web_port),
                        "--strictPort",
                    ],
                    cwd=ROOT / "apps" / "web",
                )
            )
            deadline = time.monotonic() + args.startup_timeout
            while not supervisor.stopping:
                supervisor.check_children(active)
                if ready(args.api_port, args.web_port) and all(
                    subprocess.run(
                        [
                            python,
                            "-m",
                            "marketing_agents.workers.worker_health",
                            "--health-file",
                            str(logs / f"{kind}-worker.health.json"),
                        ],
                        cwd=state,
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    ).returncode
                    == 0
                    for kind in ("run", "scheduler")
                ):
                    break
                if time.monotonic() >= deadline:
                    raise NativeStartupError("native_readiness_timeout: inspect local logs")
                time.sleep(0.25)
            if not supervisor.stopping:
                print(f"Local mock platform ready: http://127.0.0.1:{args.web_port}", flush=True)
                print(f"Local process logs: {logs}", flush=True)
            while not args.smoke and not supervisor.stopping:
                supervisor.check_children(active)
                time.sleep(0.25)
        finally:
            supervisor.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data" / "native")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=5173)
    parser.add_argument("--startup-timeout", type=float, default=90)
    parser.add_argument("--node", type=Path, help="Explicit installed Node 24.20.0 binary")
    parser.add_argument("--smoke", action="store_true", help="Wait for readiness, then stop")
    args = parser.parse_args(argv)
    if not 1 <= args.startup_timeout <= 300:
        parser.error("startup timeout must be between 1 and 300 seconds")
    try:
        run(args)
    except (NativeStartupError, OSError) as error:
        print(
            str(error) if isinstance(error, NativeStartupError) else "native_process_start_failed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
