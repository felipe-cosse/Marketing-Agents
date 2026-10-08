"""Owned two-container orchestration for the native AC-15 kernel audit.

The target remains non-root/capability-free. Only the separate read-only collector
gets BPF/PERFMON. Every phase is matched to the source-declared command inventory;
collector snapshots, not target self-report, determine its audit evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import subprocess
import time

from scripts.ac_15_kernel_client import (
    EVENT_PREFIX,
    LINE_LIMIT,
    canonical,
    command_digest,
    counter_names,
    parse_json,
    phase_summary,
    validate_counters,
)
from scripts.del_05_clean_state import VerificationFailure, require


def validate_snapshot(value: dict, event: str = "SNAPSHOT") -> dict:
    keys = {
        "schema_version",
        "event",
        "counters",
        "pending_empty",
        "recursion_misses",
        "overflow",
        "conservation",
        "monotonic",
        "sequence",
    }
    if event == "DONE":
        keys |= {"detached", "all_fds_closed"}
    require(
        type(value) is dict
        and set(value) == keys
        and value["event"] == event
        and type(value["schema_version"]) is int
        and value["schema_version"] == 1,
        "kernel_snapshot_schema",
    )
    require(
        type(value["sequence"]) is int and 0 < value["sequence"] <= 256,
        "kernel_snapshot_sequence_invalid",
    )
    require(
        value["pending_empty"] is True
        and value["overflow"] is False
        and value["conservation"] is True
        and value["monotonic"] is True,
        "kernel_snapshot_incomplete",
    )
    misses = value["recursion_misses"]
    require(
        type(misses) is dict
        and set(misses) == {"entry", "exit"}
        and all(type(count) is int and count == 0 for count in misses.values()),
        "kernel_recursion_missed",
    )
    if event == "DONE":
        require(
            value["detached"] is True and value["all_fds_closed"] is True,
            "kernel_detach_incomplete",
        )
    validate_counters(value["counters"])
    return value["counters"]


def validate_ready(value: dict, target: int) -> None:
    require(
        type(value) is dict
        and set(value)
        == {
            "event",
            "schema_version",
            "attached",
            "own_cgroup",
            "target_cgroup",
            "schema",
            "programs",
            "maps",
            "counter_names",
            "source",
            "self_controls",
            "baseline",
        }
        and value.get("event") == "READY"
        and type(value["schema_version"]) is int
        and value.get("schema_version") == 1
        and value.get("attached") is True
        and type(value.get("target_cgroup")) is int
        and value["target_cgroup"] == target
        and type(value.get("own_cgroup")) is int
        and 0 < value["own_cgroup"] < 2**64
        and value["own_cgroup"] != target
        and value.get("counter_names") == list(counter_names()),
        "kernel_collector_ready_invalid",
    )
    schema = value.get("schema")
    require(
        type(schema) is dict
        and set(schema)
        == {
            "function_btf_id",
            "regs8_offset",
            "pt_regs_size",
            "btf_sha256",
            "compat_symbol_present",
        }
        and type(schema["function_btf_id"]) is int
        and schema["function_btf_id"] > 0
        and schema["regs8_offset"] == 64
        and type(schema["regs8_offset"]) is int
        and type(schema["pt_regs_size"]) is int
        and schema["pt_regs_size"] >= 248
        and type(schema["btf_sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", schema["btf_sha256"])
        and type(schema["compat_symbol_present"]) is bool,
        "kernel_btf_schema_invalid",
    )
    programs, maps = value.get("programs"), value.get("maps")
    require(
        type(maps) is dict
        and set(maps) == {"counters", "pending"}
        and all(type(v) is int and 0 < v < 2**32 for v in maps.values())
        and len(set(maps.values())) == 2,
        "kernel_map_identity_invalid",
    )
    require(
        type(programs) is dict and set(programs) == {"entry", "exit"}, "kernel_programs_missing"
    )
    for role, attach in (("entry", 24), ("exit", 25)):
        item = programs[role]
        require(
            type(item) is dict
            and set(item)
            == {
                "hook",
                "attach_type",
                "attach_btf_id",
                "program_id",
                "attach_btf_obj_id",
                "instruction_count",
                "bytecode_sha256",
                "verifier_log",
            }
            and item.get("hook") == "f" + role + "/do_el0_svc"
            and item.get("attach_type") == attach
            and item.get("attach_btf_id") == schema["function_btf_id"]
            and all(
                type(item.get(key)) is int and 0 < item[key] < 2**32
                for key in ("program_id", "attach_btf_obj_id", "attach_btf_id", "attach_type")
            )
            and type(item.get("instruction_count")) is int
            and 0 < item["instruction_count"] <= 8192
            and type(item.get("bytecode_sha256")) is str
            and re.fullmatch(r"[0-9a-f]{64}", item["bytecode_sha256"]),
            "kernel_program_identity_invalid",
        )
        stats = item.get("verifier_log")
        require(
            type(stats) is dict
            and set(stats)
            == {"level", "required_bytes", "sha256", "processed_instructions", "instruction_limit"}
            and type(stats["level"]) is int
            and stats.get("level") == 4
            and type(stats.get("required_bytes")) is int
            and 0 < stats["required_bytes"] <= 65536
            and type(stats.get("sha256")) is str
            and re.fullmatch(r"[0-9a-f]{64}", stats["sha256"])
            and type(stats.get("processed_instructions")) is int
            and type(stats.get("instruction_limit")) is int
            and 0 < stats["processed_instructions"] <= stats["instruction_limit"] <= 1000000,
            "kernel_verifier_evidence_invalid",
        )
    require(
        programs["entry"]["program_id"] != programs["exit"]["program_id"]
        and programs["entry"]["attach_btf_obj_id"] == programs["exit"]["attach_btf_obj_id"],
        "kernel_program_identity_collision",
    )
    require(
        value["self_controls"] == {"connect": 64, "prctl": 16, "seccomp": 16}
        and all(type(v) is int for v in value["self_controls"].values()),
        "kernel_self_controls_invalid",
    )
    require(
        type(value["baseline"]) is dict
        and set(value["baseline"])
        == {
            "sequence",
            "counters",
            "pending_empty",
            "recursion_misses",
            "overflow",
            "conservation",
            "monotonic",
        },
        "kernel_baseline_schema_invalid",
    )
    baseline = {"event": "SNAPSHOT", "schema_version": 1, **value["baseline"]}
    require(
        validate_snapshot(baseline) == dict.fromkeys(counter_names(), 0)
        and baseline["sequence"] == 1,
        "kernel_baseline_invalid",
    )


def validate_phase(summary: dict, index: int, suite: str) -> None:
    delta = summary["counters"]
    require(
        all(
            delta[key] == 0
            for key in (
                "unclassified",
                "read_error",
                "vector_error",
                "regs_read_error",
                "control_error",
                "control_read_error",
                "control_state_error",
            )
        ),
        "kernel_unclassified_or_incomplete_phase",
    )
    canary = index - (3 if suite == "browser" else 0)
    external, filters = (1, 1) if canary == 3 else (1, 0) if canary in (0, 2) else (0, 0)
    require(
        delta["external"] == external
        and delta["filter_changes"] == filters
        and summary["rejected"] == external + filters,
        "kernel_phase_rejection_mismatch",
    )
    if canary == 1:
        require(delta["unix"] == 1, "kernel_unix_health_control_missing")
    if suite == "browser" and index == 2:
        require(
            all(delta[key] > 0 for key in ("loopback", "unix", "kernel_ipc")),
            "kernel_browser_transport_health_missing",
        )


class KernelHost:
    def __init__(self, runner, image: str, manifest: dict, suite: str):
        from scripts import ac_15_offline as offline

        self.offline, self.runner = offline, runner
        self.image, self.manifest, self.suite = image, manifest, suite
        self.deadline = time.monotonic() + offline.HOST_TIMEOUT
        self.records = {
            role: {
                "name": f"marketing-agents-{runner.owner}-{suite}-{role}",
                "identity": None,
                "client": None,
                "attempted": False,
                "stdout": bytearray(),
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "stdout_sha256": hashlib.sha256(),
                "stderr_sha256": hashlib.sha256(),
                "stdout_eof": False,
                "stderr_eof": False,
                "inactive": False,
                "cessation_confirmed": False,
            }
            for role in ("target", "collector")
        }
        self.evidence = {"schema_version": 1, "suite": suite, "phases": [], "ok": False}
        runner.report.setdefault("kernel_observers", []).append(self.evidence)
        self.cleaned = None
        self.sequence = 1

    def command(self, name, argv, *, timeout=10, check=True):
        return self.runner.command(name, argv, timeout=timeout, check=check)

    def validate_container(self, value, role):
        record = self.records[role]
        name, owner = record["name"], self.runner.owner
        config, host = value["Config"], value["HostConfig"]
        require(
            self.offline.CONTAINER_ID.fullmatch(value["Id"])
            and value["Name"] == "/" + name
            and value["Image"] == self.image
            and config["Labels"].get(self.offline.LABEL + "owner") == owner,
            "kernel_container_identity_invalid",
        )
        require(
            host.get("Init") is True
            and host.get("Privileged") is False
            and host.get("NetworkMode") == "none"
            and not host.get("PortBindings")
            and not host.get("Binds")
            and not value.get("Mounts")
            and not host.get("Devices")
            and not host.get("DeviceRequests")
            and set(value["NetworkSettings"].get("Networks", {})) <= {"none"}
            and host.get("PidMode", "") == ""
            and host.get("UTSMode", "") == ""
            and host.get("IpcMode") == "private"
            and host.get("CgroupnsMode") == "private"
            and host.get("PidsLimit") == 1024
            and host.get("ShmSize") == 1024**3
            and set(host.get("CapDrop", [])) == {"ALL"}
            and host.get("SecurityOpt") == ["no-new-privileges"],
            "kernel_container_boundary_invalid",
        )
        if role == "collector":
            require(
                config["User"] == "0:0"
                and host.get("ReadonlyRootfs") is True
                and set(host.get("CapAdd", [])) == {"CAP_BPF", "CAP_PERFMON"},
                "kernel_collector_privilege_invalid",
            )
        else:
            self.offline.validate_container(value, name=name, owner=owner, image=self.image)
            require(not host.get("CapAdd"), "kernel_target_capabilities_forbidden")
        return value["Id"]

    def inspect(self, role):
        record = self.records[role]
        values = json.loads(
            self.command(
                "kernel-inspect-" + role,
                ["docker", "container", "inspect", record["identity"] or record["name"]],
            ).stdout
        )
        require(len(values) == 1, "kernel_container_ambiguous")
        identity = self.validate_container(values[0], role)
        require(
            record["identity"] is None or identity == record["identity"], "kernel_container_changed"
        )
        return values[0]

    def launch(self, role, target=None):
        record = self.records[role]
        argv = [
            "docker",
            "create",
            "--pull=never",
            "--interactive",
            "--init",
            "--name",
            record["name"],
            "--label",
            self.offline.LABEL + "owner=" + self.runner.owner,
            "--network",
            "none",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--cgroupns=private",
            "--ipc=private",
            "--pids-limit=1024",
            "--shm-size=1g",
            "--user",
            "0:0" if role == "collector" else "10001:10001",
        ]
        if role == "collector":
            argv += ["--read-only", "--cap-add=BPF", "--cap-add=PERFMON", "--entrypoint", "python"]
        for key, value in self.offline.SAFE_ENV.items():
            argv += ["--env", key + "=" + value]
        for key in ("source_commit", "source_tree", "source_sha256"):
            argv += ["--env", "AC15_" + key.upper() + "=" + self.manifest[key]]
        argv += [self.image]
        argv += (
            ["-m", "scripts.ac_15_kernel_collector", str(target)]
            if role == "collector"
            else ["--suite", self.suite]
        )
        record["attempted"] = True
        identity = self.command("kernel-create-" + role, argv).stdout.decode().strip()
        require(self.offline.CONTAINER_ID.fullmatch(identity), "kernel_created_identity_invalid")
        record["identity"] = identity
        self.inspect(role)
        record["client"] = subprocess.Popen(
            ["docker", "start", "--attach", "--interactive", identity],
            cwd=self.runner.root,
            env=self.runner.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def send(self, role, value):
        client = self.records[role]["client"]
        require(client is not None and client.poll() is None, "kernel_client_exited")
        payload = value.encode() + b"\n" if isinstance(value, str) else canonical(value) + b"\n"
        require(len(payload) <= LINE_LIMIT, "kernel_protocol_line_bound")
        client.stdin.write(payload)
        client.stdin.flush()

    def read_streams(self, role, deadline, *, final=False):
        """One bounded pump, including the still-held other role's failure pipes."""
        remaining = deadline - time.monotonic()
        require(remaining > 0, "kernel_host_deadline")
        streams = {}
        for other, item in self.records.items():
            if item["client"] is not None and not item["inactive"]:
                for kind in ("stdout", "stderr"):
                    if not item[kind + "_eof"]:
                        streams[getattr(item["client"], kind)] = (other, kind)
        ready, _, _ = select.select(list(streams), [], [], min(0.1, remaining))
        for stream in ready:
            other, kind = streams[stream]
            item = self.records[other]
            data = os.read(stream.fileno(), 4096)
            if not data:
                item[kind + "_eof"] = True
                # Only the finishing role may end its stdout without a next
                # protocol line. The other role must remain held and alive.
                if kind == "stdout":
                    require(
                        (final and other == role) or b"\n" in item["stdout"],
                        "kernel_observer_or_target_eof",
                    )
                continue
            item[kind + "_bytes"] += len(data)
            item[kind + "_sha256"].update(data)
            require(item[kind + "_bytes"] <= 1024**2, "kernel_protocol_output_bound")
            if kind == "stderr":
                raise VerificationFailure("kernel_process_stderr")
            require(not final, "kernel_process_failed_or_extra_output")
            item["stdout"].extend(data)
            require(len(item["stdout"]) <= LINE_LIMIT, "kernel_protocol_line_bound")
            require(other == role, "kernel_unsolicited_process_output")

    def line(self, role):
        record = self.records[role]
        while b"\n" not in record["stdout"]:
            require(not record["stdout_eof"], "kernel_observer_or_target_eof")
            self.read_streams(role, self.deadline)
        raw, _, remainder = record["stdout"].partition(b"\n")
        record["stdout"] = bytearray(remainder)
        return bytes(raw)

    def event(self, role, expected):
        raw = self.line(role)
        if role == "target":
            if expected != "RESULT" and raw.startswith(self.offline.PREFIX.encode()):
                partial = parse_json(raw[len(self.offline.PREFIX) :])
                require(
                    partial.get("suite") == self.suite
                    and all(
                        partial.get(key) == self.manifest[key]
                        for key in ("source_commit", "source_tree", "source_sha256")
                    ),
                    "kernel_partial_source_mismatch",
                )
                self.runner.report["suites"].append(partial)
                raise VerificationFailure("kernel_target_failed_before_complete_audit")
            prefix = (self.offline.PREFIX if expected == "RESULT" else EVENT_PREFIX).encode()
            require(raw.startswith(prefix), "kernel_target_protocol_invalid")
            raw = raw[len(prefix) :]
        value = parse_json(raw)
        if role == "collector" and value.get("event") == "FAILED":
            # Collector emits categorical diagnostics only; don't copy unknown
            # fields through to a durable report.
            self.evidence["collector_failed"] = True
            self.evidence["collector_failure_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
        require(expected == "RESULT" or value.get("event") == expected, "kernel_unexpected_event")
        return value

    def retain_failed_phase(self):
        """Keep the already-finished child's report, never wait for another phase.

        The phase rejection remains authoritative even if this small diagnostic
        grace expires or the target reports something unexpected. The parent
        has received END and can now project its captured output. No next BEGIN
        is acknowledged, no RELEASE is sent, and no deadline is extended.
        """
        original_deadline = self.deadline
        self.deadline = min(original_deadline, time.monotonic() + 2)
        try:
            partial = self.event("target", "RESULT")
            require(
                partial.get("ok") is False
                and partial.get("suite") == self.suite
                and all(
                    partial.get(key) == self.manifest[key]
                    for key in ("source_commit", "source_tree", "source_sha256")
                ),
                "kernel_failed_phase_result_unbound",
            )
            self.runner.report["suites"].append(partial)
            self.evidence["failed_phase_result_retained"] = True
        except (VerificationFailure, OSError, ValueError, KeyError) as error:
            self.evidence["failed_phase_result_retained"] = False
            self.evidence["failed_phase_result_error"] = type(error).__name__
        finally:
            self.deadline = original_deadline

    def snapshot(self):
        self.send("collector", "SNAPSHOT")
        value = self.event("collector", "SNAPSHOT")
        counters = validate_snapshot(value)
        self.sequence += 1
        require(value["sequence"] == self.sequence, "kernel_snapshot_sequence_mismatch")
        return counters

    def quiescence(self, value):
        require(
            type(value) is dict
            and set(value)
            == {
                "schema_version",
                "cgroup_inode",
                "cgroup_unchanged",
                "remaining_tasks",
                "trusted_process_count",
                "trusted_thread_count",
                "baseline_unchanged",
            }
            and value
            == {
                "schema_version": 1,
                "cgroup_inode": self.target_cgroup,
                "cgroup_unchanged": True,
                "remaining_tasks": 0,
                "trusted_process_count": 2,
                "trusted_thread_count": 2,
                "baseline_unchanged": True,
            }
            and all(
                type(value[key]) is int
                for key in (
                    "schema_version",
                    "cgroup_inode",
                    "remaining_tasks",
                    "trusted_process_count",
                    "trusted_thread_count",
                )
            )
            and value["cgroup_unchanged"] is True
            and value["baseline_unchanged"] is True,
            "kernel_target_not_quiescent",
        )

    def finish_process(self, role):
        record = self.records[role]
        client = record["client"]
        remaining = self.deadline - time.monotonic()
        require(remaining > 0 and not record["stdout"], "kernel_final_output_incomplete")
        deadline = min(self.deadline, time.monotonic() + 10)
        while not (record["stdout_eof"] and record["stderr_eof"] and client.poll() is not None):
            self.read_streams(role, deadline, final=True)
        client.wait(timeout=max(0.001, min(1, deadline - time.monotonic())))
        require(
            client.returncode == 0,
            "kernel_process_failed_or_extra_output",
        )
        record["inactive"] = True
        state = self.inspect(role)["State"]
        require(state["Running"] is False and state["ExitCode"] == 0, "kernel_container_not_exited")
        record["cessation_confirmed"] = True

    def run(self):
        self.launch("target")
        ready = self.event("target", "READY")
        require(
            set(ready) == {"event", "suite", "cgroup_inode", "source", "qualification"}
            and ready["suite"] == self.suite
            and ready["source"]
            == {
                key: self.manifest[key] for key in ("source_commit", "source_tree", "source_sha256")
            }
            and type(ready["cgroup_inode"]) is int
            and 0 < ready["cgroup_inode"] < 2**64,
            "kernel_target_ready_invalid",
        )
        self.target_cgroup = ready["cgroup_inode"]
        qualification = ready["qualification"]
        fixed = {
            "schema_version": 1,
            "native_abi": "linux_aarch64_little_64",
            "native_guard_installed": True,
            "native_elf_exit": 42,
            "core_file_limit_zero": True,
            "inherited_sockets": 0,
            "inherited_io_uring": 0,
            "cgroup_inode": self.target_cgroup,
            "cgroup_read_only": True,
            "cgroup_subgroups": 0,
            "trusted_process_count": 2,
            "trusted_thread_count": 2,
            "source_sha256": self.manifest["files"]["scripts/ac_15_kernel_boundary.py"]["sha256"],
        }
        require(
            type(qualification) is dict
            and set(qualification)
            == set(fixed) | {"foreign_elf_rejection", "io_uring_setup_rejection"}
            and all(
                type(qualification[key]) is type(value) and qualification[key] == value
                for key, value in fixed.items()
            )
            and qualification["foreign_elf_rejection"] in {"ENOEXEC", "SIGSYS"}
            and qualification["io_uring_setup_rejection"] in {"EPERM", "ENOSYS"},
            "kernel_target_qualification_invalid",
        )
        self.evidence["target"] = ready
        self.launch("collector", self.target_cgroup)
        collector = self.event("collector", "READY")
        validate_ready(collector, self.target_cgroup)
        require(collector["source"] == ready["source"], "kernel_collector_source_mismatch")
        self.evidence["collector"] = collector
        previous = self.snapshot()
        require(not any(previous.values()), "kernel_initial_counters_nonzero")
        self.send("target", {"event": "ATTACHED"})
        commands = self.offline.audit_phase_commands(self.suite)
        for phase, command in enumerate(commands, 1):
            begin = self.event("target", "BEGIN")
            require(
                set(begin) == {"event", "phase", "command_sha256", "quiescence"}
                and type(begin["phase"]) is int
                and begin["phase"] == phase
                and begin["command_sha256"] == command_digest(command),
                "kernel_phase_inventory_mismatch",
            )
            self.quiescence(begin["quiescence"])
            baseline = self.snapshot()
            require(baseline == previous, "kernel_activity_outside_phase")
            self.send("target", {"event": "BEGIN", "phase": phase})
            end = self.event("target", "END")
            require(
                set(end) == {"event", "phase", "quiescence"}
                and type(end["phase"]) is int
                and end["phase"] == phase,
                "kernel_phase_end_mismatch",
            )
            self.quiescence(end["quiescence"])
            current = self.snapshot()
            summary = phase_summary(previous, current)
            self.evidence["phases"].append(
                {
                    "phase": phase,
                    "command_sha256": command_digest(command),
                    "summary": summary,
                    "quiescence": end["quiescence"],
                }
            )
            previous = current
            self.send("target", {"event": "END", "phase": phase, "summary": summary})
            try:
                validate_phase(summary, phase - 1, self.suite)
            except VerificationFailure:
                self.retain_failed_phase()
                raise
        hold = self.event("target", "HOLD")
        require(
            set(hold) == {"event", "phase", "quiescence"}
            and type(hold["phase"]) is int
            and hold["phase"] == len(commands),
            "kernel_hold_incomplete",
        )
        self.quiescence(hold["quiescence"])
        self.send("collector", "STOP")
        done = self.event("collector", "DONE")
        self.sequence += 1
        require(done.get("sequence") == self.sequence, "kernel_final_sequence_mismatch")
        require(validate_snapshot(done, "DONE") == previous, "kernel_final_counters_changed")
        self.evidence["completion"] = done
        self.finish_process("collector")
        self.send("target", {"event": "RELEASE"})
        result = self.event("target", "RESULT")
        self.runner.report["suites"].append(result)
        require(
            result.get("suite") == self.suite
            and result.get("ok") is True
            and all(
                result.get(key) == self.manifest[key]
                for key in ("source_commit", "source_tree", "source_sha256")
            ),
            "kernel_suite_failed_or_source_changed",
        )
        actual = result.get("commands", [])
        require(
            [entry["argv"] for entry in actual]
            == [list(command) for command in self.offline.SUITES[self.suite]],
            "suite_command_inventory_mismatch",
        )
        summaries = [phase["summary"] for phase in self.evidence["phases"][-len(actual) :]]
        require(
            [entry["network_syscalls"] for entry in actual] == summaries
            and all(item["rejected"] == 0 for item in summaries),
            "kernel_suite_evidence_mismatch",
        )
        self.finish_process("target")
        self.evidence["ok"] = True

    def cleanup(self, deadline):
        if self.cleaned is not None:
            return self.cleaned
        result = {"ok": True, "roles": {}}
        collector_record = self.records["collector"]
        collector_ceased = (
            not collector_record["attempted"] or collector_record["cessation_confirmed"]
        )
        for role in ("collector", "target"):
            record = self.records[role]
            evidence = result["roles"][role] = {"ok": False}
            try:
                if record["attempted"]:
                    if role == "target" and not collector_ceased:
                        # The inode is only trusted while this exact target's
                        # cgroup is occupied. Do not free it beneath a possibly
                        # attached collector. Reap the bridge independently;
                        # the target failure hold does not depend on stdin EOF.
                        evidence.update(
                            retained=True,
                            retained_reason="collector_cessation_unconfirmed",
                            container_id=record["identity"],
                            container_name=record["name"],
                            hold="original_target_deadline_only",
                        )
                        raise VerificationFailure("kernel_collector_cessation_unconfirmed")
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, "kernel_cleanup_deadline")
                    identity = record["identity"] or record["name"]
                    check = self.command(
                        "kernel-cleanup-inspect",
                        ["docker", "container", "inspect", identity],
                        timeout=min(8, remaining),
                        check=False,
                    )
                    if check.returncode == 0:
                        items = json.loads(check.stdout)
                        require(len(items) == 1, "kernel_cleanup_identity_ambiguous")
                        identity = self.validate_container(items[0], role)
                        require(
                            record["identity"] is None or identity == record["identity"],
                            "kernel_cleanup_identity_changed",
                        )
                        if role == "collector":
                            collector_ceased = items[0].get("State", {}).get("Running") is False
                        remaining = deadline - time.monotonic()
                        require(remaining > 0, "kernel_cleanup_deadline")
                        try:
                            self.command(
                                "kernel-remove-owned",
                                ["docker", "container", "rm", "--force", identity],
                                timeout=min(8, remaining),
                            )
                        except (VerificationFailure, OSError, subprocess.SubprocessError) as error:
                            # A timed-out/failed remove has an unknown daemon
                            # outcome. Still inspect independently; stopped or
                            # absent may safely release the target even though
                            # this cleanup operation remains a reported failure.
                            evidence["remove_failure"] = type(error).__name__
                            result["ok"] = False
                    else:
                        require(
                            re.fullmatch(
                                r"Error(?: response from daemon)?: No such (?:object|container): "
                                + re.escape(identity),
                                check.stderr.decode().strip(),
                            ),
                            "kernel_cleanup_inspect_failed",
                        )
                        if role == "collector":
                            collector_ceased = True
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, "kernel_cleanup_deadline")
                    absent = self.command(
                        "kernel-confirm-removed",
                        ["docker", "container", "inspect", identity],
                        timeout=min(5, remaining),
                        check=False,
                    )
                    if absent.returncode == 0:
                        items = json.loads(absent.stdout)
                        require(len(items) == 1, "kernel_cleanup_identity_ambiguous")
                        require(
                            self.validate_container(items[0], role) == identity,
                            "kernel_cleanup_identity_changed",
                        )
                        if role == "collector":
                            collector_ceased = items[0].get("State", {}).get("Running") is False
                    require(
                        absent.returncode != 0
                        and re.fullmatch(
                            r"Error(?: response from daemon)?: No such (?:object|container): "
                            + re.escape(identity),
                            absent.stderr.decode().strip(),
                        ),
                        "kernel_owned_cleanup_unconfirmed",
                    )
                    if role == "collector":
                        collector_ceased = True
                evidence["ok"] = "remove_failure" not in evidence
            except (
                VerificationFailure,
                OSError,
                ValueError,
                KeyError,
                subprocess.SubprocessError,
            ) as error:
                evidence["failure"] = type(error).__name__
                result["ok"] = False
            finally:
                if role == "collector":
                    evidence["cessation_confirmed"] = collector_ceased
                client = record["client"]
                if client is not None:
                    try:
                        if client.poll() is None:
                            client.kill()
                        client.wait(timeout=max(0.001, min(2, deadline - time.monotonic())))
                        evidence["attached_client_reaped"] = True
                    except (OSError, subprocess.SubprocessError):
                        evidence["attached_client_reaped"] = False
                        result["ok"] = False
                    for stream in (client.stdin, client.stdout, client.stderr):
                        try:
                            stream.close()
                        except OSError:
                            evidence["stream_close_failed"] = True
                            result["ok"] = False
                evidence["stderr_bytes"] = record["stderr_bytes"]
                evidence["stderr_sha256"] = record["stderr_sha256"].hexdigest()
                evidence["stdout_bytes"] = record["stdout_bytes"]
                evidence["stdout_sha256"] = record["stdout_sha256"].hexdigest()
        self.cleaned = result
        self.evidence["cleanup"] = result
        return result
