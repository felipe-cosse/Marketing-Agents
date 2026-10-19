"""Target-side, bounded parent-only protocol for the AC-15 kernel observer.

Children receive no protocol stdin. Raw child output is captured, never forwarded
as protocol. The trusted parent remains alive until the host detaches both hooks.
No fallback to ptrace or unaudited execution exists in this client.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import time

from scripts.del_05_clean_state import VerificationFailure, require

EVENT_PREFIX = "AC15_AUDIT "
LINE_LIMIT = 65536


class AuditedCommandFailure(VerificationFailure):
    """Preserve captured child output for the existing sanitized projection."""

    def __init__(self, code: str, result: subprocess.CompletedProcess) -> None:
        super().__init__(code)
        self.result = result


def parse_json(line: str | bytes) -> dict:
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "kernel_protocol_duplicate_key")
            value[key] = item
        return value

    def constant(_value):
        raise VerificationFailure("kernel_protocol_nonfinite_number")

    require(len(line) <= LINE_LIMIT, "kernel_protocol_line_bound")
    value = json.loads(line, object_pairs_hook=pairs, parse_constant=constant)
    require(type(value) is dict, "kernel_protocol_object_required")
    return value


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def command_digest(command: tuple[str, ...]) -> str:
    normalized = list(command)
    if normalized and normalized[0] == sys.executable:
        normalized[0] = "{qualified-python}"
    return hashlib.sha256(canonical(normalized)).hexdigest()


def counter_names() -> tuple[str, ...]:
    from scripts.ac_15_kernel_bytecode import COUNTER_NAMES

    return COUNTER_NAMES


def validate_kernel_configuration(value: dict) -> None:
    """Validate the collector's live proof, never infer disabled from absence."""
    require(
        type(value) is dict
        and set(value) == {"source", "release", "config_sha256", "anon_vma_name", "policy"}
        and type(value["source"]) is str
        and value["source"] == "/proc/config.gz"
        and type(value["release"]) is str
        and re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", value["release"])
        and type(value["config_sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", value["config_sha256"])
        and type(value["anon_vma_name"]) is str
        and value["anon_vma_name"] == "disabled"
        and type(value["policy"]) is str
        and value["policy"] == "disabled_requires_einval",
        "kernel_configuration_unqualified",
    )


def validate_counters(value: dict) -> None:
    from scripts.ac_15_kernel_bytecode import READ_DIAGNOSTIC_NAMES

    require(type(value) is dict and set(value) == set(counter_names()), "kernel_counter_schema")
    require(
        all(type(count) is int and 0 <= count < 2**63 for count in value.values()),
        "kernel_counter_overflow_or_type",
    )
    require(
        value["messages"]
        == sum(
            value[key]
            for key in (
                "loopback",
                "external",
                "unix",
                "kernel_ipc",
                "connected_send",
                "unclassified",
                "read_error",
            )
        ),
        "kernel_message_conservation",
    )
    require(
        value["output_queries_started"] == value["output_queries_completed"],
        "kernel_output_query_incomplete",
    )
    require(
        value["vma_unsupported_started"] == value["vma_unsupported_completed"],
        "kernel_vma_unsupported_incomplete",
    )
    require(
        sum(value[key] for key in READ_DIAGNOSTIC_NAMES) == value["control_read_error"],
        "kernel_control_read_diagnostic_conservation",
    )


def phase_summary(before: dict, after: dict) -> dict:
    validate_counters(before)
    validate_counters(after)
    require(all(after[key] >= before[key] for key in before), "kernel_counter_regression")
    delta = {key: after[key] - before[key] for key in before}
    validate_counters(delta)
    rejected = sum(
        delta[key]
        for key in (
            "external",
            "unclassified",
            "read_error",
            "vector_error",
            "regs_read_error",
            "filter_changes",
            "control_error",
            "control_read_error",
            "control_state_error",
        )
    )
    evidence = {"schema_version": 2, "observer": "kernel-cgroup-v1", "counters": delta}
    return {
        **evidence,
        "sha256": hashlib.sha256(canonical(evidence)).hexdigest(),
        **{
            key: delta[key]
            for key in (
                "loopback",
                "unix",
                "kernel_ipc",
                "connected_send",
                "filter_changes",
                "control_queries",
                "safe_prctl",
            )
        },
        "rejected": rejected,
    }


class KernelAuditClient:
    def __init__(self, suite: str, deadline: float):
        require(math.isfinite(deadline), "kernel_deadline_invalid")
        self.suite, self.deadline = suite, deadline
        self.phase = 0
        self.started = False
        self.closed = False
        self.kernel_configuration = None

    def exchange(self, value: dict, expected: str) -> dict:
        # The original container budget reserves 20s after the shared command
        # deadline for audit/source finalization; no command gains this reserve.
        require(time.monotonic() < self.deadline + 20, "kernel_control_deadline_exceeded")
        payload = EVENT_PREFIX.encode() + canonical(value) + b"\n"
        require(len(payload) <= LINE_LIMIT, "kernel_protocol_line_bound")
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        line = sys.stdin.buffer.readline(LINE_LIMIT + 1)
        require(line.endswith(b"\n") and len(line) <= LINE_LIMIT, "kernel_protocol_reply_missing")
        reply = parse_json(line)
        require(reply.get("event") == expected, "kernel_protocol_reply_unexpected")
        return reply

    def begin(self, source: dict) -> dict:
        from scripts.ac_15_kernel_boundary import qualify_target

        require(not self.started and not self.closed, "kernel_observer_already_started")
        proof = qualify_target()
        reply = self.exchange(
            {
                "event": "READY",
                "suite": self.suite,
                "cgroup_inode": proof["cgroup_inode"],
                "source": {
                    key: source[key] for key in ("source_commit", "source_tree", "source_sha256")
                },
                "qualification": proof,
            },
            "ATTACHED",
        )
        require(set(reply) == {"event", "kernel_configuration"}, "kernel_attach_reply_invalid")
        validate_kernel_configuration(reply["kernel_configuration"])
        self.kernel_configuration = dict(reply["kernel_configuration"])
        self.started = True
        return {
            "observer": "kernel-cgroup-v1",
            "qualification": proof,
            "kernel_configuration": dict(self.kernel_configuration),
        }

    def collect(self, command: tuple[str, ...], *, timeout: float):
        from scripts.ac_15_kernel_boundary import quiescent_target

        require(self.started and not self.closed, "kernel_observer_not_active")
        require(
            type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
            "kernel_command_timeout_invalid",
        )
        self.phase += 1
        proof = quiescent_target()
        reply = self.exchange(
            {
                "event": "BEGIN",
                "phase": self.phase,
                "command_sha256": command_digest(command),
                "quiescence": proof,
            },
            "BEGIN",
        )
        require(
            reply == {"event": "BEGIN", "phase": self.phase} and type(reply.get("phase")) is int,
            "kernel_begin_reply_invalid",
        )
        remaining = min(timeout, self.deadline - time.monotonic())
        require(remaining > 0, "suite_deadline_exceeded")
        result = subprocess.run(
            command, capture_output=True, stdin=subprocess.DEVNULL, timeout=remaining, check=False
        )
        try:
            proof = quiescent_target()
            reply = self.exchange({"event": "END", "phase": self.phase, "quiescence": proof}, "END")
            require(
                set(reply) == {"event", "phase", "summary"}
                and reply["phase"] == self.phase
                and type(reply["phase"]) is int,
                "kernel_end_reply_invalid",
            )
            summary = reply["summary"]
            require(
                type(summary) is dict and type(summary.get("counters")) is dict,
                "kernel_summary_missing",
            )
            zero = dict.fromkeys(counter_names(), 0)
            require(summary == phase_summary(zero, summary["counters"]), "kernel_summary_mismatch")
            if summary["counters"]["vma_unsupported_started"]:
                validate_kernel_configuration(self.kernel_configuration)
        except (VerificationFailure, ValueError, OSError) as error:
            raise AuditedCommandFailure(
                str(error) if isinstance(error, VerificationFailure) else type(error).__name__,
                result,
            ) from error
        return result, summary

    def finish(self) -> None:
        from scripts.ac_15_kernel_boundary import quiescent_target

        require(self.started and not self.closed, "kernel_observer_not_active")
        reply = self.exchange(
            {"event": "HOLD", "phase": self.phase, "quiescence": quiescent_target()}, "RELEASE"
        )
        require(reply == {"event": "RELEASE"}, "kernel_release_reply_invalid")
        self.closed = True
