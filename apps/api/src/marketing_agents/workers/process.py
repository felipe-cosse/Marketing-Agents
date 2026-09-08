"""Bounded process supervision, signal handling, and local health witnesses."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal
from uuid import uuid4

from marketing_agents.config import get_settings
from marketing_agents.workers.runtime.composition import build_runtime
from marketing_agents.workers.runtime.run_loop import RunWorker
from marketing_agents.workers.runtime.scheduler import SchedulerWorker


def _health(path: Path | None, worker_id: str, status: str) -> None:
    if path is not None:
        # Explicit task-scoped health files contain no application payload or key.
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "worker_id": worker_id,
                "status": status,
                "updated_at": time.time(),
            }
        )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(payload)


async def run_process(kind: Literal["run", "scheduler"], args: argparse.Namespace) -> None:
    runtime = await build_runtime(get_settings())
    worker_id = args.worker_id or f"worker.{kind}.{uuid4().hex}"
    worker = RunWorker(runtime, worker_id) if kind == "run" else SchedulerWorker(runtime, worker_id)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()

    def stop() -> None:
        worker.stop_claiming()
        stopping.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop)
    try:
        _health(args.health_file, worker_id, "ready")
        print(json.dumps({"service": kind, "worker_id": worker_id, "status": "ready"}), flush=True)
        while not stopping.is_set():
            advancement = asyncio.create_task(worker.drain_once())
            stop_waiter = asyncio.create_task(stopping.wait())
            try:
                done, _ = await asyncio.wait(
                    (advancement, stop_waiter), return_when=asyncio.FIRST_COMPLETED
                )
                if stop_waiter in done and not advancement.done():
                    with suppress(TimeoutError):
                        await asyncio.wait_for(advancement, timeout=args.shutdown_seconds)
                else:
                    await advancement
            finally:
                stop_waiter.cancel()
                if not advancement.done():
                    advancement.cancel()
                await asyncio.gather(stop_waiter, advancement, return_exceptions=True)
            _health(args.health_file, worker_id, "stopping" if stopping.is_set() else "ready")
            if args.once or stopping.is_set():
                break
            with suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=args.poll_seconds)
    finally:
        worker.stop_claiming()
        _health(args.health_file, worker_id, "stopped")
        await runtime.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main(kind: Literal["run", "scheduler"]) -> int:
    parser = argparse.ArgumentParser(description=f"Local {kind} worker")
    parser.add_argument("--worker-id")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--shutdown-seconds", type=float, default=20.0)
    parser.add_argument("--health-file", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 0.05 <= args.poll_seconds <= 30 or not 0.1 <= args.shutdown_seconds <= 45:
        parser.error("poll/shutdown values must be bounded")
    try:
        asyncio.run(run_process(kind, args))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print(
            f"{kind}_worker_failed: inspect readiness and durable recovery state", file=sys.stderr
        )
        return 1
    return 0
