"""AC-15: exact-source acquisition, then complete test suites without kernel egress.

Acquisition is explicit and may download frozen build dependencies. ``run`` never
builds, pulls, mounts host paths, or acquires dependencies. It requires a prebuilt
immutable image for the current clean commit. Reports contain only fixed fields,
source/image identities, command exit codes and output digests, never raw logs.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ac_15_browser_transport import (  # noqa: E402
    install_browser_transport,
    prove_browser_transport,
    transport_canary_commands,
)
from scripts.ac_15_kernel_client import (  # noqa: E402
    AuditedCommandFailure as TracedCommandFailure,
)
from scripts.ac_15_kernel_client import (  # noqa: E402
    KernelAuditClient,
)
from scripts.del_05_clean_state import (  # noqa: E402
    VerificationFailure,
    clean_environment,
    export_archive,
    require,
    tree_fingerprint,
    validate_docker_endpoint,
    validate_docker_engine_version,
    validate_exported_tree,
    verification_signals,
)
from scripts.del_05_offline_diagnostics import project_diagnostics  # noqa: E402

LABEL = "io.marketing-agents.ac15."
PREFIX = "AC15_RESULT "
SAFE_ENV = {
    "APP_ENV": "local",
    "AUTH_MODE": "local",
    "LLM_PROVIDER": "mock",
    "CONNECTOR_MODE": "mock",
    "ALLOW_EXTERNAL_NETWORK": "false",
    "REAL_LLM_OPT_IN": "false",
    "REAL_CONNECTOR_OPT_IN": "false",
    "UV_OFFLINE": "1",
    "UV_NO_SYNC": "1",
    "UV_FROZEN": "1",
    "COREPACK_ENABLE_NETWORK": "0",
    "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
    "COREPACK_ENABLE_AUTO_PIN": "0",
    "PNPM_CONFIG_OFFLINE": "true",
    "PNPM_CONFIG_UPDATE_NOTIFIER": "false",
    "NPM_CONFIG_OFFLINE": "true",
    "PLAYWRIGHT_BROWSERS_PATH": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    # Ruff's parallel workers can disappear before ptrace decodes their final
    # stop. Keep every file/check, but avoid that diagnosed shutdown race.
    "RAYON_NUM_THREADS": "1",
    "CI": "true",
}
SUITES = {
    "backend": (("make", "verify-del-05-offline-backend"),),
    "frontend": (
        ("make", "verify-del-05-offline-web"),
        (
            "node",
            "--test",
            "tests/network/browser_network_policy.test.mjs",
            "tests/network/node_network_guard.test.mjs",
            "tests/network/obj_06_control_witness.test.mjs",
            "tests/network/playwright_network_isolation.test.mjs",
            "tests/network/web_e2e_inventory.test.mjs",
        ),
    ),
    "browser": (
        ("node", "apps/web/scripts/run-browser-network-canary.mjs"),
        ("node", "apps/web/scripts/run-web-e2e.mjs"),
    ),
    "native": (("make", "verify-native-offline"),),
}
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SUITE_TIMEOUT = 1700
CONTAINER_TIMEOUT = SUITE_TIMEOUT + 20
HOST_TIMEOUT = SUITE_TIMEOUT + 40
MAX_TRACE_BYTES = 512 * 1024 * 1024
MAX_TRACE_RECORD_BYTES = 1024 * 1024
MAX_TRACE_PENDING_PIDS = 1024
TRACE_SYSCALLS = "connect,sendto,sendmsg,sendmmsg,seccomp,prctl"
TRACE_CALL_PATTERN = "(?:" + TRACE_SYSCALLS.replace(",", "|") + ")"
# Linux v6.1 include/uapi/linux/capability.h; never accept invented CAP_* names.
TRACE_CAPABILITIES = frozenset(
    [
        "CAP_CHOWN",
        "CAP_DAC_OVERRIDE",
        "CAP_DAC_READ_SEARCH",
        "CAP_FOWNER",
        "CAP_FSETID",
        "CAP_KILL",
        "CAP_SETGID",
        "CAP_SETUID",
        "CAP_SETPCAP",
        "CAP_LINUX_IMMUTABLE",
        "CAP_NET_BIND_SERVICE",
        "CAP_NET_BROADCAST",
        "CAP_NET_ADMIN",
        "CAP_NET_RAW",
        "CAP_IPC_LOCK",
        "CAP_IPC_OWNER",
        "CAP_SYS_MODULE",
        "CAP_SYS_RAWIO",
        "CAP_SYS_CHROOT",
        "CAP_SYS_PTRACE",
        "CAP_SYS_PACCT",
        "CAP_SYS_ADMIN",
        "CAP_SYS_BOOT",
        "CAP_SYS_NICE",
        "CAP_SYS_RESOURCE",
        "CAP_SYS_TIME",
        "CAP_SYS_TTY_CONFIG",
        "CAP_MKNOD",
        "CAP_LEASE",
        "CAP_AUDIT_WRITE",
        "CAP_AUDIT_CONTROL",
        "CAP_SETFCAP",
        "CAP_MAC_OVERRIDE",
        "CAP_MAC_ADMIN",
        "CAP_SYSLOG",
        "CAP_WAKE_ALARM",
        "CAP_BLOCK_SUSPEND",
        "CAP_AUDIT_READ",
        "CAP_PERFMON",
        "CAP_BPF",
        "CAP_CHECKPOINT_RESTORE",
    ]
)

# Linux UAPI numbers for the two pinned-image architectures only. This child
# runs after kernel_namespace proves no external route; it installs a filter
# whose ERRNO verdict masks connect from a lower-priority strace TRACE verdict.
SECCOMP_MASK_CANARY = """
import ctypes, errno, json, platform, socket
numbers = {"aarch64": (277, 203), "x86_64": (317, 42)}
machine = platform.machine()
assert machine in numbers, "unsupported canary architecture"
seccomp_number, connect_number = numbers[machine]
class Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]
class Program(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]
filters = (Filter * 4)(Filter(0x20, 0, 0, 0),
    Filter(0x15, 0, 1, connect_number),
    Filter(0x06, 0, 0, 0x00050000 | errno.EPERM), Filter(0x06, 0, 0, 0x7fff0000))
program = Program(len(filters), filters)
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
assert libc.prctl(38, 1, 0, 0, 0) == 0, "no-new-privileges failed"
assert libc.syscall(seccomp_number, 1, 0, ctypes.byref(program)) == 0, "filter failed"
with socket.socket() as client:
    try:
        client.connect(("192.0.2.1", 9))
    except OSError as error:
        assert error.errno == errno.EPERM, "connect was not masked"
    else:
        raise AssertionError("connect unexpectedly succeeded")
print(json.dumps({"filter_installed": True, "masked_connect_errno": errno.EPERM}))
"""


def validate_caller_environment(environment: dict[str, str]) -> None:
    expected = {
        "APP_ENV": {"local", "test"},
        "AUTH_MODE": {"local"},
        "LLM_PROVIDER": {"mock"},
        "CONNECTOR_MODE": {"mock"},
        "ALLOW_EXTERNAL_NETWORK": {"false", "0", "no", "off"},
        "REAL_LLM_OPT_IN": {"false", "0", "no", "off"},
        "REAL_CONNECTOR_OPT_IN": {"false", "0", "no", "off"},
    }
    for name, value in environment.items():
        if name.upper() in expected:
            require(
                value.strip().lower() in expected[name.upper()], "unsafe_caller_runtime_environment"
            )


def _trace_records(text: str | Iterable[str]):
    """Reassemble strace's per-thread split calls before inspecting any arguments."""
    pending: dict[str | None, tuple[str, str]] = {}
    lines = text.splitlines() if isinstance(text, str) else text
    for raw in lines:
        require(
            len(raw.encode("utf-8")) <= MAX_TRACE_RECORD_BYTES,
            "network_trace_record_exceeds_bound",
        )
        if not raw.strip():
            continue
        prefix = re.fullmatch(r"\s*(?:(\d+)\s+|\[pid\s+(\d+)\]\s+)?(.*)", raw)
        require(prefix is not None, "network_trace_unclassified_record")
        assert prefix is not None
        pid, bracketed_pid, body = prefix.groups()
        pid = pid or bracketed_pid
        resumed = re.fullmatch(r"<\.\.\. (" + TRACE_CALL_PATTERN + r") resumed>(.*)", body)
        if resumed is not None:
            name, remainder = resumed.groups()
            require(pid in pending, "network_trace_orphan_resume")
            previous_name, previous = pending.pop(pid)
            require(name == previous_name, "network_trace_mismatched_resume")
            require(
                len(previous.encode("utf-8")) + len(remainder.encode("utf-8"))
                <= MAX_TRACE_RECORD_BYTES,
                "network_trace_assembled_record_exceeds_bound",
            )
            body = previous + remainder
        call = re.match("(" + TRACE_CALL_PATTERN + r")\(", body)
        if call is not None:
            require(pid not in pending, "network_trace_overlapping_call")
            unfinished = re.search(r"\s*<unfinished \.\.\.>$", body)
            if unfinished is not None:
                require(
                    len(pending) < MAX_TRACE_PENDING_PIDS,
                    "network_trace_pending_pids_exceed_bound",
                )
                pending[pid] = (call.group(1), body[: unfinished.start()])
            else:
                # Completion must be a complete syscall, never an opaque partial
                # tail that happens to contain an already-decoded local address.
                require(re.search(r"\)\s*=\s*.+$", body), "network_trace_incomplete_call")
                yield body
            continue
        # Fixed strace lifecycle grammar only. Diagnostics, unsupported resumed
        # syscalls and arbitrary lines remain errors, not blanket ignore rules.
        require(
            re.fullmatch(
                r"\+\+\+ (?:exited with \d+|killed by SIG[A-Z0-9]+(?: \(core dumped\))?"
                r"|superseded by execve in pid \d+) \+\+\+",
                body,
            )
            or re.fullmatch(r"--- SIG[A-Z0-9]+(?: \{.*\})? ---", body),
            "network_trace_unclassified_record",
        )
    require(not pending, "network_trace_unfinished_call")


def _trace_fields(value: str) -> list[str]:
    """Split one decoded struct/vector level without treating payload text as fields."""
    fields: list[str] = []
    stack: list[str] = []
    quote = escaped = False
    start = 0
    pairs = {")": "(", "]": "[", "}": "{"}
    for index, character in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quote = False
        elif character == '"':
            quote = True
        elif character in "([{":
            stack.append(character)
        elif character in ")]}":
            require(stack and stack.pop() == pairs[character], "network_trace_vector_unparsed")
        elif character == "," and not stack:
            fields.append(value[start:index].strip())
            start = index + 1
    require(not stack and not quote, "network_trace_vector_unparsed")
    if value.strip():
        fields.append(value[start:].strip())
    require(all(fields), "network_trace_vector_unparsed")
    return fields


def _validate_sendmmsg_vector(line: str) -> None:
    call = re.fullmatch(r"sendmmsg\((.*)\)\s*=\s*.+", line)
    require(call is not None, "network_trace_vector_unparsed")
    assert call is not None
    arguments = _trace_fields(call.group(1))
    require(len(arguments) == 4 and arguments[2].isdigit(), "network_trace_vector_unparsed")
    vector = arguments[1]
    require(vector.startswith("[") and vector.endswith("]"), "network_trace_vector_unparsed")
    messages = _trace_fields(vector[1:-1])
    require(len(messages) == int(arguments[2]), "network_trace_vector_unparsed")
    for message in messages:
        require(message.startswith("{") and message.endswith("}"), "network_trace_vector_unparsed")
        fields = _trace_fields(message[1:-1])
        headers = [
            field.removeprefix("msg_hdr=") for field in fields if field.startswith("msg_hdr=")
        ]
        require(
            len(headers) == 1
            and all(
                field.startswith("msg_hdr=") or re.fullmatch(r"msg_len=\d+", field)
                for field in fields
            ),
            "network_trace_vector_unparsed",
        )
        header = headers[0]
        require(header.startswith("{") and header.endswith("}"), "network_trace_vector_unparsed")
        members = _trace_fields(header[1:-1])
        require(
            all(
                re.match(
                    r"(?:msg_name|msg_namelen|msg_iov|msg_iovlen|msg_control|msg_controllen|msg_flags)=",
                    member,
                )
                for member in members
            )
            and sum(member.startswith("msg_name=") for member in members) == 1,
            "network_trace_vector_unparsed",
        )


def _trace_control_operation(line: str) -> str:
    """No descendant may install a filter that masks the syscall observer."""
    call = re.fullmatch(r"(seccomp|prctl)\((.*)\)\s*=\s*.+", line)
    require(call is not None, "network_trace_control_unparsed")
    assert call is not None
    name, raw_arguments = call.groups()
    arguments = _trace_fields(raw_arguments)
    require(arguments, "network_trace_control_unparsed")
    operation = arguments[0]
    if name == "seccomp":
        if operation in {"SECCOMP_SET_MODE_FILTER", "SECCOMP_SET_MODE_STRICT"}:
            return "filter_changes"  # Failed and opaque installation attempts also fail.
        require(
            len(arguments) == 3
            and arguments[1] == "0"
            and operation in {"SECCOMP_GET_ACTION_AVAIL", "SECCOMP_GET_NOTIF_SIZES"},
            "network_trace_seccomp_operation_unparsed",
        )
        if operation == "SECCOMP_GET_ACTION_AVAIL":
            require(
                re.fullmatch(
                    r"\[SECCOMP_RET_(?:KILL_PROCESS|KILL_THREAD|TRAP|ERRNO|USER_NOTIF|"
                    r"TRACE|LOG|ALLOW)\]",
                    arguments[2],
                ),
                "network_trace_seccomp_query_unparsed",
            )
        else:
            require(
                re.fullmatch(
                    r"\{seccomp_notif=\d+, seccomp_notif_resp=\d+, seccomp_data=\d+\}",
                    arguments[2],
                ),
                "network_trace_seccomp_query_unparsed",
            )
        return "control_queries"
    if operation == "PR_SET_SECCOMP":
        return "filter_changes"
    if len(arguments) == 1 and operation in {
        "PR_GET_SECCOMP",
        "PR_GET_DUMPABLE",
        "PR_SVE_GET_VL",
        "PR_SME_GET_VL",
    }:
        return "control_queries"
    if operation == "PR_CAPBSET_READ":
        # commoncap.c reads cap_bset, including invalid-capability probes. This
        # does not authorize PR_CAPBSET_DROP or any capability-changing operation.
        require(
            len(arguments) == 2
            and (
                arguments[1] in TRACE_CAPABILITIES
                or re.fullmatch(r"0x[0-9a-fA-F]{1,8} /\* CAP_\?\?\? \*/", arguments[1])
            ),
            "network_trace_capability_query_unparsed",
        )
        return "control_queries"
    if operation == "PR_SET_VMA":
        # kernel/sys.c's ANON_NAME sub-operation only labels memory mappings;
        # unlike SET_SECCOMP/SET_SYSCALL_USER_DISPATCH, it cannot mask syscalls.
        require(
            len(arguments) == 5
            and arguments[1] == "PR_SET_VMA_ANON_NAME"
            and re.fullmatch(r"0x[0-9a-fA-F]{1,16}", arguments[2])
            and re.fullmatch(r"[0-9]{1,20}", arguments[3])
            and int(arguments[3]) <= 2**64 - 1
            and re.fullmatch(r'"(?:\\.|[^"\\])*"(?:\.\.\.)?', arguments[4]),
            "network_trace_vma_label_unparsed",
        )
        return "safe_prctl"
    if arguments == ["PR_GET_NO_NEW_PRIVS", "0", "0", "0", "0"]:
        return "control_queries"
    if arguments == ["PR_SET_NO_NEW_PRIVS", "1", "0", "0", "0"]:
        return "safe_prctl"  # Strengthens the existing no-new-privileges boundary.
    if operation in {"PR_SET_NAME", "PR_GET_NAME"} and len(arguments) == 2:
        require(
            re.fullmatch(r'"(?:\\.|[^"\\])*"(?:\.\.\.)?|0x[0-9a-fA-F]+|NULL', arguments[1]),
            "network_trace_prctl_argument_unparsed",
        )
        return "safe_prctl" if operation == "PR_SET_NAME" else "control_queries"
    # In particular, PR_SET_SYSCALL_USER_DISPATCH is not a harmless alternative
    # to seccomp: it can also suppress kernel syscall observation.
    raise VerificationFailure("network_trace_prctl_operation_unparsed")


def _trace_summary_lines(lines: Iterable[str], digest) -> dict:
    """Classify every record; the caller hashes the exact original byte stream."""
    counts = dict.fromkeys(
        (
            "loopback",
            "unix",
            "kernel_ipc",
            "connected_send",
            "rejected",
            "filter_changes",
            "control_queries",
            "safe_prctl",
        ),
        0,
    )
    for line in _trace_records(lines):
        if line.startswith(("seccomp(", "prctl(")):
            category = _trace_control_operation(line)
            counts[category] += 1
            if category == "filter_changes":
                counts["rejected"] += 1
            continue

        # Keep only IP address literals; payload/path strings cannot impersonate
        # decoded sockaddr fields, even when reviewing a non--s-0 fixture.
        def scrub(match: re.Match[str], original: str = line) -> str:
            preceding = original[: match.start()]
            return (
                match.group()
                if (
                    preceding.endswith("inet_addr(")
                    or re.search(r"inet_pton\(AF_INET6,\s*$", preceding)
                )
                else '""'
            )

        line = re.sub(r'"(?:\\.|[^"\\])*"', scrub, line)
        if line.startswith("sendmmsg("):
            _validate_sendmmsg_vector(line)
        families = re.findall(r"\bsa_family=([^,}\s]+)", line)
        require(
            set(families)
            <= {"AF_INET", "AF_INET6", "AF_UNIX", "AF_LOCAL", "AF_NETLINK", "AF_UNSPEC"},
            "network_trace_family_unparsed",
        )
        names = re.findall(r"\bmsg_name=([^,}\s]+)", line)
        require(
            all(value == "NULL" or value.startswith("{sa_family=") for value in names),
            "network_trace_destination_unparsed",
        )
        if "AF_NETLINK" in families:
            pids = re.findall(r"\bnl_pid=([^,}\s]+)", line)
            require(
                len(pids) == families.count("AF_NETLINK") and set(pids) == {"0"},
                "network_trace_kernel_destination_unparsed",
            )
        if re.search(r"sa_family=AF_INET6?\b", line):
            addresses = re.findall(r'inet_addr\("([^"\n]+)"\)', line)
            addresses += re.findall(r'inet_pton\(AF_INET6, "([^"\n]+)"', line)
            require(
                len(addresses) == families.count("AF_INET") + families.count("AF_INET6"),
                "network_trace_address_unparsed",
            )
            for address in addresses:
                value = ipaddress.ip_address(address)
                loopback = value.is_loopback or (
                    isinstance(value, ipaddress.IPv6Address)
                    and value.ipv4_mapped is not None
                    and value.ipv4_mapped.is_loopback
                )
                counts["loopback" if loopback else "rejected"] += 1
        elif "AF_UNIX" in line or "AF_LOCAL" in line:
            counts["unix"] += 1
        elif "sa_family=AF_NETLINK" in line and re.search(r"\bnl_pid=0\b", line):
            counts["kernel_ipc"] += 1
        elif (
            "AF_UNSPEC" in line
            or ("sendto(" in line and re.search(r", NULL, 0\)\s*(?:=|<unfinished)", line))
            or (("sendmsg(" in line or "sendmmsg(" in line) and "msg_name=NULL" in line)
        ):
            counts["connected_send"] += 1
        else:
            raise VerificationFailure("network_trace_destination_unparsed")
    return {"schema_version": 1, "sha256": digest.hexdigest(), **counts}


def _trace_summary_stream(source: BinaryIO) -> dict:
    """Bound physical reads, decoded records and pending calls independently.

    The physical byte limit includes line endings. Decoding/splitlines is only
    applied to one bounded LF-delimited physical record, preserving the existing
    treatment of CRLF, bare CR and Unicode line separators. Hashing happens before
    this normalization. A completed final record need not have a trailing newline.
    """
    digest = hashlib.sha256()

    def lines():
        total = 0
        while raw := source.readline(min(MAX_TRACE_RECORD_BYTES + 1, MAX_TRACE_BYTES - total + 1)):
            total += len(raw)
            require(total <= MAX_TRACE_BYTES, "network_trace_exceeds_bound")
            require(len(raw) <= MAX_TRACE_RECORD_BYTES, "network_trace_record_exceeds_bound")
            digest.update(raw)
            yield from raw.decode("utf-8", errors="strict").splitlines()

    return _trace_summary_lines(lines(), digest)


def trace_summary(data: bytes) -> dict:
    """Byte API retained for fixtures; use the identical bounded stream parser."""
    require(len(data) <= MAX_TRACE_BYTES, "network_trace_exceeds_bound")
    return _trace_summary_stream(io.BytesIO(data))


def trace_file_summary(path: Path) -> dict:
    """Audit a completed trace without allocating its complete raw/decoded contents."""
    with path.open("rb") as source:
        return _trace_summary_stream(source)


ACTIVE_OBSERVER: KernelAuditClient | None = None


def collect_strace_fixture(
    command: tuple[str, ...], *, timeout: float
) -> tuple[subprocess.CompletedProcess, dict]:
    with tempfile.TemporaryDirectory(prefix="marketing-agents-ac15-trace-") as temporary:
        trace = Path(temporary) / "network.trace"
        trace.touch(mode=0o600)
        argv = [
            "strace",
            "--seccomp-bpf",
            "-f",
            "-qq",
            "-v",
            "-s",
            "0",
            "-e",
            "trace=" + TRACE_SYSCALLS,
            "-o",
            str(trace),
            "--",
            *command,
        ]
        result = subprocess.run(argv, cwd=ROOT, capture_output=True, timeout=timeout, check=False)
        try:
            require(
                not any(line.startswith(b"strace:") for line in result.stderr.splitlines()),
                "network_trace_setup_or_runtime_diagnostic",
            )
            summary = trace_file_summary(trace)
        except (VerificationFailure, OSError, ValueError) as error:
            raise TracedCommandFailure(safe_failure_code(error), result) from error
        return result, summary


def traced_command(
    command: tuple[str, ...], *, timeout: float
) -> tuple[subprocess.CompletedProcess, dict]:
    result, summary = collect_trace(command, timeout=timeout)
    if summary["rejected"] != 0:
        raise TracedCommandFailure("external_network_syscall_observed", result)
    return result, summary


def collect_trace(command: tuple[str, ...], *, timeout: float):
    """Production observation is kernel-backed; missing control never falls back."""
    require(ACTIVE_OBSERVER is not None, "kernel_observer_missing")
    return ACTIVE_OBSERVER.collect(command, timeout=timeout)


def start_kernel_observer(suite: str, deadline: float, manifest: dict) -> dict:
    global ACTIVE_OBSERVER
    require(ACTIVE_OBSERVER is None, "kernel_observer_already_active")
    observer = KernelAuditClient(suite, deadline)
    # A collector may attach before its READY/ATTACHED handshake fails. Keep
    # this lifetime owner available to the failed hold even without that ACK.
    ACTIVE_OBSERVER = observer
    proof = observer.begin(manifest)
    return proof


def finish_kernel_observer() -> None:
    global ACTIVE_OBSERVER
    observer = ACTIVE_OBSERVER
    if observer is not None:
        observer.finish()
        ACTIVE_OBSERVER = None


def failed_command_evidence(
    command: tuple[str, ...], *, elapsed: float, error: Exception, suite: str
) -> dict:
    """Project safe evidence; complete output capture never implies test/audit success."""
    evidence: dict = {"argv": list(command), "duration_seconds": round(elapsed, 3)}
    if isinstance(error, TracedCommandFailure):
        stdout, stderr = error.result.stdout, error.result.stderr
        evidence.update(exit_code=error.result.returncode, output_complete=True)
    elif isinstance(error, subprocess.TimeoutExpired):
        stdout, stderr = error.stdout, error.stderr
        evidence["output_complete"] = False
    else:
        return evidence

    def encoded(value: bytes | str | None) -> bytes:
        return value if isinstance(value, bytes) else (value or "").encode(errors="replace")

    evidence.update(
        stdout_sha256=hashlib.sha256(encoded(stdout)).hexdigest(),
        stderr_sha256=hashlib.sha256(encoded(stderr)).hexdigest(),
    )
    if suite == "backend":
        evidence["diagnostics"] = project_diagnostics(stdout, stderr, ROOT)
    return evidence


def test_summary(output: bytes, kind: str) -> dict:
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output.decode(errors="replace"))
    if kind == "vitest":
        passed = re.findall(r"\bTests\s+([0-9]+) passed", text)
    elif kind == "node":
        passed = re.findall(r"^(?:#|\u2139) pass ([0-9]+)$", text, re.MULTILINE)
    else:
        passed = re.findall(r"\b([0-9]+) passed\b", text)
    require(passed and all(int(value) > 0 for value in passed), "nonzero_test_summary_required")
    return {
        "kind": kind,
        "passed": sum(int(value) for value in passed),
        "summary_count": len(passed),
    }


def trace_canary_commands() -> tuple[tuple[str, ...], ...]:
    """One fixed inventory shared by target execution and independent host checks."""
    command = (
        sys.executable,
        "-I",
        "-S",
        "-c",
        "import socket; s=socket.socket(); s.connect_ex(('192.0.2.1',9)); s.close()",
    )
    positive = (
        sys.executable,
        "-I",
        "-S",
        "-c",
        "import socket,tempfile; d=tempfile.TemporaryDirectory(); s=socket.socket(socket.AF_UNIX); "
        "s.bind(d.name+'/peer'); s.listen(1); c=socket.socket(socket.AF_UNIX); "
        "c.connect(d.name+'/peer'); c.close(); s.close(); d.cleanup()",
    )
    grandchild = (
        "import subprocess,sys; result=subprocess.run([sys.executable,'-I','-S','-c',"
        + repr(command[-1])
        + "]); raise SystemExit(result.returncode)"
    )
    return (
        command,
        positive,
        (sys.executable, "-I", "-S", "-c", grandchild),
        (sys.executable, "-I", "-S", "-c", SECCOMP_MASK_CANARY),
    )


def audit_phase_commands(suite: str) -> tuple[tuple[str, ...], ...]:
    return (
        (transport_canary_commands() if suite == "browser" else ())
        + trace_canary_commands()
        + SUITES[suite]
    )


def prove_trace_guard() -> dict:
    commands = trace_canary_commands()
    result, summary = collect_trace(commands[0], timeout=10)
    require(result.returncode == 0 and summary["rejected"] == 1, "trace_guard_canary_not_rejected")
    result, summary = collect_trace(commands[1], timeout=10)
    require(
        result.returncode == 0 and summary["rejected"] == 0 and summary["unix"] == 1,
        "trace_guard_positive_canary_failed",
    )
    result, summary = collect_trace(commands[2], timeout=10)
    require(
        result.returncode == 0 and summary["rejected"] == 1,
        "trace_guard_grandchild_not_rejected",
    )
    result, summary = collect_trace(commands[3], timeout=10)
    require(
        result.returncode == 0
        and json.loads(result.stdout) == {"filter_installed": True, "masked_connect_errno": 1}
        and summary["filter_changes"] == 1
        and summary["rejected"] == 2
        and summary["counters"]["external"] == 1,
        "trace_guard_masking_filter_not_rejected",
    )
    return {
        "swallowed_child_syscall_rejected": True,
        "unix_child_trace_observed": True,
        "swallowed_grandchild_syscall_rejected": True,
        "masking_filter_install_rejected": True,
    }


def source_manifest(root: Path, commit: str, tree: str) -> dict:
    return {
        "schema_version": 1,
        "source_commit": commit,
        "source_tree": tree,
        "source_sha256": tree_fingerprint(root),
        "files": {
            path.relative_to(root).as_posix(): {
                "mode": path.stat().st_mode & 0o777,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(root.rglob("*"))
            if path.is_file()
        },
    }


def verify_source(root: Path, manifest: dict) -> None:
    digest = hashlib.sha256()
    require(manifest["schema_version"] == 1 and manifest["files"], "invalid_source_manifest")
    for relative, expected in sorted(manifest["files"].items(), key=lambda item: Path(item[0])):
        path = root / relative
        require(
            not Path(relative).is_absolute() and ".." not in Path(relative).parts,
            "invalid_source_path",
        )
        require(
            path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(root.resolve()),
            "missing_source_file",
        )
        content = path.read_bytes()
        mode = path.stat().st_mode & 0o777
        require(
            hashlib.sha256(content).hexdigest() == expected["sha256"] and mode == expected["mode"],
            "source_content_changed",
        )
        for field in (relative.encode(), str(mode).encode(), content):
            digest.update(len(field).to_bytes(8, "big"))
            digest.update(field)
    require(digest.hexdigest() == manifest["source_sha256"], "source_fingerprint_mismatch")
    generated_directories = {
        ".venv",
        "node_modules",
        "apps/web/node_modules",
        "tools/api-contract/node_modules",
        ".cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "apps/web/dist",
        "apps/web/coverage",
        "apps/web/playwright-report",
        "apps/web/test-results",
    }
    for directory, folders, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for folder in tuple(folders):
            path = parent / folder
            relative = path.relative_to(root).as_posix()
            if relative in generated_directories:
                folders.remove(folder)
                continue
            require(not path.is_symlink(), "untracked_source_symlink")
        for name in files:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if relative in manifest["files"] or relative == ".coverage":
                continue
            require(not path.is_symlink(), "untracked_source_symlink")
            require(
                parent.name == "__pycache__"
                and re.fullmatch(r"[A-Za-z0-9_]+\.cpython-312(?:\.opt-[12])?\.pyc", name),
                "untracked_execution_input",
            )


def kernel_namespace(
    sysfs: Path = Path("/sys/class/net"),
    ipv4: Path = Path("/proc/net/route"),
    ipv6: Path = Path("/proc/net/ipv6_route"),
) -> dict:
    """Prove only loopback is usable; dormant kernel tunnel devices are not routes."""
    states = []
    for path in sorted(sysfs.iterdir()):
        if not path.is_dir():
            require(path.is_file() and not path.is_symlink(), "kernel_sysfs_entry_unparsed")
            continue  # sysfs control files such as bonding_masters are not interfaces.
        require(re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", path.name), "kernel_interface_name_invalid")
        flags_text = (path / "flags").read_text().strip()
        require(re.fullmatch(r"0x[0-9a-fA-F]{1,8}", flags_text), "kernel_interface_flags_unparsed")
        flags = int(flags_text, 16)
        state = (path / "operstate").read_text().strip()
        if path.name == "lo":
            require(
                flags & 0x1 and flags & 0x8 and state in {"up", "unknown"},
                "kernel_loopback_not_active",
            )
        else:
            require(not flags & 0x1 and state == "down", "kernel_has_active_nonloopback_interface")
        states.append({"name": path.name, "flags": flags, "operstate": state})
    require(any(entry["name"] == "lo" for entry in states), "kernel_loopback_missing")
    routes = ipv4.read_text().splitlines()
    require(
        len(routes) == 1
        and routes[0].split()
        == [
            "Iface",
            "Destination",
            "Gateway",
            "Flags",
            "RefCnt",
            "Use",
            "Metric",
            "Mask",
            "MTU",
            "Window",
            "IRTT",
        ],
        "kernel_has_external_or_unparsed_ipv4_route",
    )
    routes6 = ipv6.read_text().splitlines()
    widths = (32, 2, 32, 2, 32, 8, 8, 8, 8)
    for route in routes6:
        fields = route.split()
        require(
            len(fields) == 10 and fields[-1] == "lo", "kernel_has_external_or_unparsed_ipv6_route"
        )
        require(
            all(
                re.fullmatch(r"[0-9a-fA-F]{" + str(width) + "}", field)
                for width, field in zip(widths, fields[:9], strict=True)
            )
            and int(fields[1], 16) <= 128
            and int(fields[3], 16) <= 128,
            "kernel_ipv6_route_unparsed",
        )
    return {
        "interfaces": [entry["name"] for entry in states],
        "interface_states": states,
        "active_interfaces": ["lo"],
        "ipv4_route_count": 0,
        "ipv6_route_count": len(routes6),
        "ipv6_route_interfaces": ["lo"] if routes6 else [],
    }


def kernel_canaries() -> dict:
    """Never contact TEST-NET until no usable external interface or route exists.

    These children deliberately do not import Python/Node test guards. They
    prove the container namespace protects child processes as well as tests.
    """
    namespace = kernel_namespace()
    environment = {name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ}
    python = (
        "import errno,json,socket; s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); "
        "s.settimeout(2); code=s.connect_ex(('192.0.2.1',9)); s.close(); "
        "print(json.dumps({'errno':code})); "
        "raise SystemExit(0 if code==errno.ENETUNREACH else 1)"
    )
    node = (
        "const net=require('node:net'); const s=net.connect({host:'192.0.2.1',port:9,family:4});"
        "s.setTimeout(2000,()=>{s.destroy();process.exit(1)});"
        "s.on('connect',()=>{s.destroy();process.exit(1)});"
        "s.on('error',e=>{console.log(JSON.stringify({code:e.code}));"
        "process.exit(e.code==='ENETUNREACH'?0:1)});"
    )
    results = {}
    for name, command in (
        ("python_child", [sys.executable, "-I", "-S", "-c", python]),
        ("node_child", ["node", "-e", node]),
    ):
        result = subprocess.run(
            command, env=environment, capture_output=True, timeout=5, check=False
        )
        require(result.returncode == 0, "kernel_egress_canary_failed")
        body = json.loads(result.stdout)
        expected = {"errno": 101} if name == "python_child" else {"code": "ENETUNREACH"}
        require(body == expected, "kernel_egress_canary_invalid")
        results[name] = body
    return {**namespace, **results}


def inside(suite: str) -> dict:
    deadline = time.monotonic() + SUITE_TIMEOUT
    require(suite in SUITES, "unknown_suite")
    require(
        all(os.environ.get(key) == value for key, value in SAFE_ENV.items()),
        "unsafe_container_environment",
    )
    require(os.getuid() == 10001, "container_requires_unprivileged_user")
    manifest = json.loads(Path("/opt/ac15/source-manifest.json").read_text())
    verify_source(ROOT, manifest)
    proof = kernel_canaries()
    if suite == "browser":
        proof["browser_transport"] = install_browser_transport()
    proof["kernel_observer"] = start_kernel_observer(suite, deadline, manifest)
    if suite == "browser":
        proof["browser_transport"].update(prove_browser_transport(collect_trace))
    proof.update(prove_trace_guard())
    if suite == "frontend":
        require(
            sorted(
                str(path.relative_to(ROOT)) for path in (ROOT / "tests/network").glob("*.test.mjs")
            )
            == list(SUITES["frontend"][1][2:]),
            "node_test_inventory_changed",
        )
    result = {key: value for key, value in manifest.items() if key != "files"}
    result.update(suite=suite, kernel=proof, commands=[], ok=False)
    for command in SUITES[suite]:
        started = time.monotonic()
        remaining = deadline - started
        try:
            require(remaining > 0, "suite_deadline_exceeded")
            run, network = traced_command(command, timeout=remaining)
            command_result = {
                "argv": list(command),
                "exit_code": run.returncode,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout_sha256": hashlib.sha256(run.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(run.stderr).hexdigest(),
                "network_syscalls": network,
            }
            result["commands"].append(command_result)
            if suite == "backend":
                command_result["diagnostics"] = project_diagnostics(run.stdout, run.stderr, ROOT)
                if run.returncode == 0:
                    summary = command_result["diagnostics"].get("pytest_summary", {})
                    require(
                        summary.get("passed", 0) > 0
                        and summary.get("failed") == 0
                        and summary.get("collection_errors") == 0,
                        "backend_pytest_summary_invalid",
                    )
            if run.returncode == 0 and suite in {"frontend", "native"}:
                kind = (
                    "node"
                    if command[0] == "node"
                    else "vitest"
                    if suite == "frontend"
                    else "pytest"
                )
                command_result["tests"] = test_summary(run.stdout, kind)
            if command[-1] == "apps/web/scripts/run-web-e2e.mjs":
                inventory = re.findall(
                    rb"Browser inventory: ([0-9]+) specs, ([0-9]+) unfiltered runners\.", run.stdout
                )
                require(
                    len(inventory) == 1 and int(inventory[0][1]) == 16,
                    "complete_browser_inventory_not_observed",
                )
                command_result["browser_inventory"] = {
                    "spec_count": int(inventory[0][0]),
                    "runner_count": int(inventory[0][1]),
                }
                if run.returncode == 0:
                    summary = test_summary(run.stdout, "playwright")
                    require(summary["summary_count"] >= 16, "browser_nonzero_summaries_missing")
                    command_result["tests"] = summary
            verify_source(ROOT, manifest)
            if run.returncode != 0:
                return result
        except (VerificationFailure, subprocess.SubprocessError, OSError, ValueError) as error:
            result["failure"] = safe_failure_code(error)
            result["failed_command"] = failed_command_evidence(
                command, elapsed=time.monotonic() - started, error=error, suite=suite
            )
            return result
    result["ok"] = True
    return result


def safe_failure_code(error: Exception) -> str:
    code = str(error) if isinstance(error, VerificationFailure) else type(error).__name__
    require(re.fullmatch(r"[A-Za-z][A-Za-z0-9_:.-]{0,127}", code), "unsafe_failure_code")
    return code


def inside_result(suite: str) -> dict:
    global ACTIVE_OBSERVER
    result = None
    expected = {
        key: os.environ.get("AC15_" + key.upper(), "")
        for key in ("source_commit", "source_tree", "source_sha256")
    }
    try:
        require(all(SHA.fullmatch(value) for value in expected.values()), "source_identity_missing")
        # Bound preflight, complete child commands, trace parsing and source
        # checks together. The short finalization reserve lets a subprocess
        # timeout preserve partial diagnostics before the whole-container alarm.
        with verification_signals(CONTAINER_TIMEOUT):
            result = inside(suite)
            if result.get("ok") is True:
                finish_kernel_observer()
            require(
                all(result[key] == value for key, value in expected.items()),
                "inside_source_mismatch",
            )
            return result
    except (
        VerificationFailure,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        OSError,
    ) as error:
        # Failure cleanup belongs to the host, which removes both owned roles.
        # Do not release a live collector using incomplete phase evidence.
        if result is not None:
            return {**result, "ok": False, "failure": safe_failure_code(error)}
        return {
            "schema_version": 1,
            **expected,
            "suite": suite,
            "commands": [],
            "ok": False,
            "failure": safe_failure_code(error),
        }


def validate_image(image: dict, expected: dict) -> str:
    identity = image.get("Id", "")
    require(IMAGE_ID.fullmatch(identity), "invalid_image_identity")
    labels = image.get("Config", {}).get("Labels", {})
    require(labels.get(LABEL + "schema") == "1", "image_not_ac15")
    for key in ("source_commit", "source_tree", "source_sha256"):
        require(
            labels.get(LABEL + key.replace("_", "-")) == expected[key],
            "image_source_identity_mismatch",
        )
    require(image.get("Config", {}).get("User") == "10001:10001", "image_user_mismatch")
    return identity


def validate_container(value: dict, *, name: str, owner: str, image: str) -> str:
    identity = value.get("Id", "")
    require(CONTAINER_ID.fullmatch(identity), "invalid_container_identity")
    require(
        value.get("Name") == "/" + name and value.get("Image") == image,
        "container_identity_mismatch",
    )
    config = value.get("Config", {})
    host = value.get("HostConfig", {})
    require(config.get("Labels", {}).get(LABEL + "owner") == owner, "container_owner_mismatch")
    require(host.get("Init") is True, "container_init_required")
    require(
        config.get("User") == "10001:10001" and not host.get("Privileged"),
        "container_privilege_mismatch",
    )
    require(
        host.get("NetworkMode") == "none"
        and not host.get("PortBindings")
        and not value.get("Mounts")
        and not host.get("Binds")
        and set(value.get("NetworkSettings", {}).get("Networks", {})) <= {"none"},
        "container_network_or_mount_mismatch",
    )
    require(
        "ALL" in host.get("CapDrop", []) and "no-new-privileges" in host.get("SecurityOpt", []),
        "container_capability_mismatch",
    )
    return identity


class OfflineRun:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        validate_caller_environment(dict(os.environ))
        self.environment = clean_environment(dict(os.environ))
        self.owner = "ac15-" + uuid.uuid4().hex
        self.containers: list[tuple[str, str]] = []
        self.kernel_sessions: list = []
        self.report: dict = {
            "schema_version": 1,
            "owner": self.owner,
            "ok": False,
            "commands": [],
            "suites": [],
            "cleanup": {"ok": False},
        }

    def command(
        self, name: str, args: list[str], *, timeout: int = 15, check: bool = True
    ) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            args,
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        self.report["commands"].append(
            {
                "name": name,
                "exit_code": completed.returncode,
                "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(completed.stderr).hexdigest(),
            }
        )
        require(not check or completed.returncode == 0, "command_failed:" + name)
        return completed

    def docker(self) -> None:
        endpoint = self.environment.get("DOCKER_HOST")
        if self.environment.get("DOCKER_CONTEXT") or not endpoint:
            contexts = json.loads(
                self.command("docker-context", ["docker", "context", "inspect"]).stdout
            )
            require(len(contexts) == 1, "ambiguous_docker_context")
            endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
        self.environment["DOCKER_HOST"] = validate_docker_endpoint(endpoint)
        self.environment.pop("DOCKER_CONTEXT", None)
        version = json.loads(
            self.command(
                "docker-version", ["docker", "version", "--format", "{{json .Server.Version}}"]
            ).stdout
        )
        validate_docker_engine_version(version)
        self.report["docker_engine_version"] = version

    def export(self, directory: Path) -> dict:
        status = self.command("clean-source", ["git", "status", "--porcelain=v1", "-z"]).stdout
        require(not status, "exact_head_requires_clean_worktree")
        commit = (
            self.command("source-commit", ["git", "rev-parse", "--verify", "HEAD^{commit}"])
            .stdout.decode()
            .strip()
        )
        tree = (
            self.command("source-tree", ["git", "rev-parse", "--verify", "HEAD^{tree}"])
            .stdout.decode()
            .strip()
        )
        require(SHA.fullmatch(commit) and SHA.fullmatch(tree), "invalid_git_identity")
        source = directory / "source"
        source.mkdir()
        archive = directory / "source.tar"
        self.command(
            "source-export", ["git", "archive", "--format=tar", f"--output={archive}", commit]
        )
        export_archive(archive, source)
        archive.unlink()
        listing = self.command(
            "source-objects", ["git", "ls-tree", "-rz", "--full-tree", commit]
        ).stdout
        validate_exported_tree(source, listing)
        manifest = source_manifest(source, commit, tree)
        (directory / "source-manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        self.report.update({key: value for key, value in manifest.items() if key != "files"})
        self.report["locks"] = {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in ("uv.lock", "pnpm-lock.yaml")
        }
        return manifest

    def image(self, identity: str, manifest: dict) -> str:
        require(IMAGE_ID.fullmatch(identity), "immutable_image_id_required")
        values = json.loads(
            self.command("inspect-image", ["docker", "image", "inspect", identity]).stdout
        )
        require(len(values) == 1, "ambiguous_image")
        require(validate_image(values[0], manifest) == identity, "image_id_changed")
        self.report["image_id"] = identity
        return identity

    def resolve_image(self, manifest: dict) -> str:
        args = ["docker", "image", "ls", "--quiet", "--no-trunc"]
        for key in ("source_commit", "source_tree", "source_sha256"):
            args.extend(
                ["--filter", "label=" + LABEL + key.replace("_", "-") + "=" + manifest[key]]
            )
        identities = set(self.command("resolve-acquired-image", args).stdout.decode().splitlines())
        require(len(identities) == 1, "exactly_one_preacquired_image_required")
        return self.image(identities.pop(), manifest)

    def acquire(self, directory: Path, manifest: dict) -> None:
        self.report["phase"] = "dependency-acquisition"
        iid = directory / "image-id"
        args = [
            "docker",
            "build",
            "--builder",
            "default",
            "--pull=false",
            "--network",
            "default",
            "--file",
            str(directory / "source/docker/ac-15.Dockerfile"),
            "--iidfile",
            str(iid),
        ]
        for key in ("source_commit", "source_tree", "source_sha256"):
            args.extend(["--build-arg", key.upper() + "=" + manifest[key]])
        args.extend(
            ["--tag", "marketing-agents-ac15:" + manifest["source_commit"][:16], str(directory)]
        )
        self.command("acquire-prebuilt-image", args, timeout=7200)
        self.image(iid.read_text().strip(), manifest)

    def execute(self, identity: str, manifest: dict, *, suite: str | None = None) -> None:
        from scripts.ac_15_kernel_host import KernelHost

        self.report["phase"] = "prebuilt-network-none-kernel-execution"
        self.image(identity, manifest)
        selected = (suite,) if suite is not None else tuple(SUITES)
        for selected_suite in selected:
            session = KernelHost(self, identity, manifest, selected_suite)
            # Register before any Docker creation; failures retain cleanup authority.
            self.kernel_sessions.append(session)
            session.run()

    def inspect_container(self, name: str) -> dict:
        values = json.loads(
            self.command("inspect-container", ["docker", "container", "inspect", name]).stdout
        )
        require(len(values) == 1, "ambiguous_container")
        return values[0]

    def cleanup(self) -> None:
        failed = False
        deadline = time.monotonic() + 30
        for session in reversed(self.kernel_sessions):
            if not session.cleanup(deadline)["ok"]:
                failed = True
        for name, image in reversed(self.containers):
            try:
                require(time.monotonic() < deadline, "owned_cleanup_deadline")
                value = self.inspect_container(name)
                identity = validate_container(value, name=name, owner=self.owner, image=image)
                self.command(
                    "remove-owned-container",
                    ["docker", "container", "rm", "--force", identity],
                    timeout=max(0.001, min(15, deadline - time.monotonic())),
                )
            except (VerificationFailure, subprocess.SubprocessError, ValueError, KeyError, OSError):
                failed = True
        self.report["cleanup"] = {
            "ok": not failed,
            "container_count": len(self.containers)
            + sum(
                sum(record["attempted"] for record in session.records.values())
                for session in self.kernel_sessions
            ),
            "images_retained": True,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("acquire", "run", "inside"))
    parser.add_argument("--image")
    parser.add_argument("--suite", choices=tuple(SUITES))
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "inside":
        require(
            args.suite is not None and args.image is None and args.report is None,
            "invalid_inside_arguments",
        )
        result = inside_result(args.suite)
        print(PREFIX + json.dumps(result, sort_keys=True), flush=True)
        if not result["ok"] and ACTIVE_OBSERVER is not None:
            # The host receives failure evidence before owned cleanup. Hold
            # this cgroup until it removes the collector first, within the
            # original whole-container deadline; never claim a failed detach.
            # A broken/reaped attach client closes stdin too. EOF is NOT proof
            # of collector cessation, so it must not release this failed target.
            try:
                hold_deadline = ACTIVE_OBSERVER.deadline + 20
                remaining = hold_deadline - time.monotonic()
                if remaining > 0:
                    with verification_signals(remaining):
                        while (remaining := hold_deadline - time.monotonic()) > 0:
                            time.sleep(min(1, remaining))
            except (VerificationFailure, OSError):
                pass
        return 0 if result["ok"] else 1
    require(
        args.mode == "run" or (args.suite is None and args.image is None),
        "invalid_runner_arguments",
    )
    if args.report is not None:
        require(
            args.report.is_absolute()
            and not args.report.resolve().is_relative_to(ROOT)
            and not args.report.exists()
            and args.report.parent.is_dir(),
            "report_must_be_new_external_file",
        )
    runner = OfflineRun(ROOT)
    try:
        with (
            verification_signals(
                HOST_TIMEOUT if args.mode == "run" and args.suite is not None else 21600
            ),
            tempfile.TemporaryDirectory(prefix="marketing-agents-ac15-") as temporary,
        ):
            directory = Path(temporary).resolve()
            require(not directory.is_relative_to(ROOT), "temporary_export_inside_source")
            manifest = runner.export(directory)
            runner.docker()
            if args.mode == "acquire":
                runner.acquire(directory, manifest)
            else:
                identity = args.image or runner.resolve_image(manifest)
                runner.execute(identity, manifest, suite=args.suite)
            current = (
                runner.command("unchanged-head", ["git", "rev-parse", "HEAD"])
                .stdout.decode()
                .strip()
            )
            require(current == manifest["source_commit"], "caller_head_changed")
            require(
                not runner.command(
                    "unchanged-source", ["git", "status", "--porcelain=v1", "-z"]
                ).stdout,
                "caller_worktree_changed",
            )
            runner.report["ok"] = True
    except (
        VerificationFailure,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        OSError,
    ) as error:
        runner.report["failure"] = (
            str(error) if isinstance(error, VerificationFailure) else type(error).__name__
        )
    finally:
        runner.cleanup()
    runner.report["ok"] = runner.report["ok"] and runner.report["cleanup"]["ok"]
    output = json.dumps(runner.report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        require(
            args.report.is_absolute() and not args.report.resolve().is_relative_to(ROOT),
            "report_must_be_external",
        )
        with args.report.open("x") as stream:
            stream.write(output)
        report_path = args.report
    else:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="marketing-agents-ac15-report-", suffix=".json", delete=False
        ) as stream:
            stream.write(output)
            report_path = Path(stream.name)
    print(output, end="")
    print("AC15 report: " + str(report_path))
    return 0 if runner.report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
