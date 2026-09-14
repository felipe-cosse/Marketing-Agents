"""OBJ-04 test-only child instrumentation, not an OS network sandbox."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.network import python_network_guard as policy

SAFE_FLAGS = {
    "APP_ENV": "local",
    "AUTH_MODE": "local",
    "LLM_PROVIDER": "mock",
    "CONNECTOR_MODE": "mock",
    "ALLOW_EXTERNAL_NETWORK": "false",
    "REAL_LLM_OPT_IN": "false",
    "REAL_CONNECTOR_OPT_IN": "false",
}
OFFLINE_TOOL_FLAGS = {
    "COREPACK_ENABLE_NETWORK": "0",
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    "COREPACK_ENABLE_AUTO_PIN": "0",
    "PNPM_CONFIG_OFFLINE": "true",
    "PNPM_CONFIG_UPDATE_NOTIFIER": "false",
}
POISON_NAMES = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "OPENAI_API_KEY",
        "REAL_LLM_API_KEY",
        "WEBHOOK_HMAC_SECRET",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NODE_OPTIONS",
        "PYTHONPATH",
    }
)


def assert_clean_environment(environment: dict[str, str], *, runtime: bool) -> None:
    # Never include values in assertion errors: the controller deliberately has
    # synthetic poison, and this helper must remain safe if reused incorrectly.
    assert not POISON_NAMES.intersection(environment), "credential/loader environment leaked"
    assert all(environment.get(key) == value for key, value in OFFLINE_TOOL_FLAGS.items()), (
        "tool acquisition/update flags are not offline"
    )
    if runtime:
        assert all(environment.get(key) == value for key, value in SAFE_FLAGS.items()), (
            "runtime safe flags changed"
        )


class GuardRecorder:
    """Deny external operations and prove canaries never reach OS delegates.

    Tripwires are installed *under* the existing guard, before canaries run.
    Even a broken guard therefore cannot turn a negative control into egress.
    Only categorical counts are persisted; no addresses, inputs, or secrets.
    """

    def __init__(self, path: Path, role: str) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "role": role,
            "pid": os.getpid(),
            "canaries_blocked": 0,
            "external_attempts": 0,
            "delegate_violations": 0,
            "application_finished": False,
        }
        self.canary_phase = True

    def save(self) -> None:
        # Each PID owns one report and the test reads it only after process exit.
        self.path.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")

    def _external(self, address: Any) -> bool:
        try:
            policy.assert_loopback(address)
        except policy.NetworkAccessBlocked:
            return True
        return False

    def install(self) -> None:
        patches = (
            (socket.socket, "connect", lambda args: args[1]),
            (socket.socket, "connect_ex", lambda args: args[1]),
            (socket.socket, "sendto", lambda args: args[-1]),
            (socket, "create_connection", lambda args: args[0]),
            (socket, "getaddrinfo", lambda args: args[0]),
        )
        for owner, name, address in patches:
            original = getattr(owner, name)

            def tripwire(
                *args: Any,
                _original: Callable = original,
                _address: Callable = address,
                **kwargs: Any,
            ) -> Any:
                if self._external(_address(args)):
                    self.data["delegate_violations"] += 1
                    self.save()
                    raise AssertionError("OBJ-04 external call reached delegate tripwire")
                return _original(*args, **kwargs)

            setattr(owner, name, tripwire)

        policy.install_network_guard()
        for owner, name, _address in patches:
            guarded = getattr(owner, name)

            def counted(*args: Any, _guarded: Callable = guarded, **kwargs: Any) -> Any:
                try:
                    return _guarded(*args, **kwargs)
                except policy.NetworkAccessBlocked:
                    field = "canaries_blocked" if self.canary_phase else "external_attempts"
                    self.data[field] += 1
                    self.save()
                    raise

            setattr(owner, name, counted)

        with socket.socket() as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            canaries = (
                lambda: socket.getaddrinfo("obj04.invalid", 443),
                lambda: socket.create_connection(("192.0.2.1", 443), timeout=0.01),
                lambda: tcp.connect(("192.0.2.2", 443)),
                lambda: tcp.connect_ex(("192.0.2.3", 443)),
                lambda: udp.sendto(b"synthetic-canary", ("192.0.2.4", 53)),
            )
            for canary in canaries:
                try:
                    canary()
                except policy.NetworkAccessBlocked:
                    continue
                raise AssertionError("OBJ-04 network canary did not fail closed")
        assert self.data["canaries_blocked"] == 5
        assert self.data["delegate_violations"] == 0
        self.canary_phase = False
        self.save()

    def finish(self) -> None:
        self.data["application_finished"] = True
        self.save()
