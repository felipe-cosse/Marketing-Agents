"""AC-18 source-bound native VMA diagnostics; never an acceptance suite.

Run with ``python -m scripts.ac_15_vma_diagnostic run --image sha256:... --report ...``.
The image must already be acquired from this clean commit by ac_15_offline.
No production inventory, bytecode, rejection rule, or container policy changes.
Untouched means no userspace read/write of that mapping, not proven residency.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scripts import ac_15_kernel_boundary as boundary
from scripts import ac_15_offline as offline
from scripts.ac_15_kernel_client import (
    KernelAuditClient,
    canonical,
    command_digest,
    counter_names,
    parse_json,
    phase_summary,
    validate_kernel_configuration,
)
from scripts.ac_15_kernel_collector import bounded_read, kernel_configuration
from scripts.ac_15_kernel_host import (
    KernelHost,
    validate_phase,
    validate_qualification,
    validate_ready,
    validate_snapshot,
)
from scripts.del_05_clean_state import VerificationFailure, require, verification_signals

CASES = ("untouched", "pre_read", "inaccessible")
MODULE = "scripts.ac_15_vma_diagnostic"
ROOT = Path(__file__).resolve().parents[1]
PROBE_SECONDS = 10
TARGET_SECONDS = 45
HOST_SECONDS = 60
CLEANUP_SECONDS = 30
SOURCE_KEYS = ("source_commit", "source_tree", "source_sha256")
FAILURES = (VerificationFailure, OSError, ValueError, KeyError, subprocess.SubprocessError)


def read_manifest() -> dict:
    # The complete source manifest is much larger than a protocol message.
    value = json.loads(bounded_read("/opt/ac15/source-manifest.json", 16 * 1024**2))
    require(type(value) is dict, "vma_diagnostic_manifest_invalid")
    return value


def case_name(case: str) -> str:
    require(type(case) is str and case in CASES, "vma_diagnostic_case_invalid")
    return "diagnostic-vma-" + case


def probe_command(case: str) -> tuple[str, ...]:
    case_name(case)
    return (sys.executable, "-m", MODULE, "probe", "--case", case)


def inherited_profile() -> None:
    require(
        not sys.flags.optimize
        and sys.platform == "linux"
        and sys.byteorder == "little"
        and platform.machine() == "aarch64"
        and ctypes.sizeof(ctypes.c_void_p) == ctypes.sizeof(ctypes.c_ulong) == 8
        and os.getuid() == os.geteuid() == 10001,
        "vma_diagnostic_native_unprivileged_required",
    )
    boundary._profile_status()
    fields = boundary._status_fields(boundary.PROC / "self/status")
    require(
        fields.get("Uid", "").split() == ["10001"] * 4
        and all(
            fields.get(key) == "0000000000000000"
            for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        )
        and fields.get("Seccomp_filters", "").isdecimal()
        and int(fields["Seccomp_filters"]) >= 2,
        "vma_diagnostic_inherited_profile_required",
    )
    offline.kernel_namespace()


def probe(case: str) -> dict:
    """Only owned anonymous pages; exactly one PR_SET_VMA, no payload output."""
    case_name(case)
    inherited_profile()
    page = os.sysconf("SC_PAGE_SIZE")
    require(4096 <= page <= 65536 and page & (page - 1) == 0, "vma_diagnostic_page_size")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = (
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_long,
    )
    libc.munmap.restype, libc.munmap.argtypes = ctypes.c_int, (ctypes.c_void_p, ctypes.c_size_t)
    libc.syscall.restype = ctypes.c_long
    libc.syscall.argtypes = (
        ctypes.c_long,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    )
    mappings = []
    try:
        for protection in (3, 0 if case == "inaccessible" else 3):
            address = libc.mmap(None, page, protection, 0x22, -1, 0)
            require(address not in (None, ctypes.c_void_p(-1).value), "vma_diagnostic_mmap_failed")
            mappings.append(address)
        region, label = mappings
        if case == "pre_read":
            require(ctypes.c_ubyte.from_address(label).value == 0, "vma_diagnostic_nonzero_page")
        ctypes.set_errno(0)
        result = libc.syscall(167, 0x53564D41, 0, region, page, label)
        error = ctypes.get_errno()
        require(
            type(result) is int and result == -1 and type(error) is int and error == 22,
            "vma_diagnostic_syscall_result_invalid",
        )
    finally:
        clean = True
        for address in reversed(mappings):
            try:
                clean = libc.munmap(address, page) == 0 and clean
            except (OSError, ValueError):
                clean = False
        require(clean, "vma_diagnostic_mapping_cleanup_failed")
    return {
        "case": case,
        "return_code": result,
        "errno": error,
        "mappings_released": True,
        "kernel_configuration": kernel_configuration(),
    }


def validate_probe(value: dict, case: str) -> None:
    require(
        type(value) is dict
        and set(value)
        == {"case", "return_code", "errno", "mappings_released", "kernel_configuration"}
        and value["case"] == case
        and type(value["return_code"]) is int
        and value["return_code"] == -1
        and type(value["errno"]) is int
        and value["errno"] == 22
        and value["mappings_released"] is True,
        "vma_diagnostic_probe_invalid",
    )
    validate_kernel_configuration(value["kernel_configuration"])


def strict_observation(summary: dict, configuration: dict) -> str | None:
    """Apply current ordinary-phase policy, without any expected-error waiver."""
    require(
        summary == phase_summary(dict.fromkeys(counter_names(), 0), summary["counters"]),
        "vma_diagnostic_summary_invalid",
    )
    try:
        # Index four in native/non-emulated inventory is ordinary, not a canary.
        validate_phase(summary, 4, "native", emulation=False, kernel_configuration=configuration)
    except VerificationFailure as error:
        return offline.safe_failure_code(error)
    return None


def validate_one_call(summary: dict) -> None:
    """Unexpected activity makes attribution incomplete, never silently allowed."""
    expected = {
        **dict.fromkeys(counter_names(), 0),
        "entry_prctl": 1,
        "safe_prctl": 1,
        "vma_unsupported_started": 1,
        "vma_unsupported_completed": 1,
    }
    require(summary["counters"] == expected, "vma_diagnostic_call_projection")


def result_body(case: str, manifest: dict) -> dict:
    return {
        "schema_version": 1,
        "diagnostic_only": True,
        "acceptance": False,
        "ok": False,
        "diagnostic_completed": False,
        "suite": case_name(case),
        "case": case,
        **{key: manifest[key] for key in SOURCE_KEYS},
    }


def inside(case: str) -> int:
    deadline = time.monotonic() + TARGET_SECONDS
    expected = {key: os.environ.get("AC15_" + key.upper(), "") for key in SOURCE_KEYS}
    result = result_body(case, expected)
    observer = None
    printed = False
    try:
        with verification_signals(TARGET_SECONDS):
            require(
                all(os.environ.get(key) == value for key, value in offline.SAFE_ENV.items()),
                "unsafe_container_environment",
            )
            manifest = read_manifest()
            require(
                all(manifest[key] == expected[key] for key in SOURCE_KEYS), "inside_source_mismatch"
            )
            offline.verify_source(ROOT, manifest)
            offline.kernel_namespace()
            observer = KernelAuditClient(case_name(case), deadline - 10)
            attached = observer.begin(manifest)
            qualification = attached["qualification"]
            configuration = attached["kernel_configuration"]
            validate_kernel_configuration(configuration)
            run, summary = observer.collect(probe_command(case), timeout=PROBE_SECONDS)
            require(
                run.returncode == 0 and not run.stderr and len(run.stdout) <= 1024,
                "vma_diagnostic_probe_failed",
            )
            observation = parse_json(run.stdout)
            validate_probe(observation, case)
            require(
                canonical(observation["kernel_configuration"]) == canonical(configuration),
                "vma_diagnostic_config_changed",
            )
            offline.verify_source(ROOT, manifest)
            failure = strict_observation(summary, configuration)
            if failure is None:
                validate_one_call(summary)
            result.update(
                diagnostic_completed=failure is None,
                probe=observation,
                summary=summary,
                strict_failure=failure,
                qualification=qualification,
                kernel_configuration=configuration,
            )
            if failure is None:
                observer.finish()
                print(offline.PREFIX + canonical(result).decode(), flush=True)
                return 0
            print(offline.PREFIX + canonical(result).decode(), flush=True)
            printed = True
            # EOF does not prove collector cessation; preserve the cgroup.
            while time.monotonic() < deadline:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except FAILURES as error:
        if not printed:
            result.update(diagnostic_completed=False, failure=offline.safe_failure_code(error))
            print(offline.PREFIX + canonical(result).decode(), flush=True)
        if observer is not None and not observer.closed:
            while time.monotonic() < deadline:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    return 1


class DiagnosticHost(KernelHost):
    def __init__(self, runner, image: str, manifest: dict, case: str):
        self.case = case
        super().__init__(runner, image, manifest, case_name(case))
        self.deadline = time.monotonic() + HOST_SECONDS
        self.evidence.update(diagnostic_only=True, acceptance=False, diagnostic_completed=False)

    def command(self, name, argv, *, timeout=10, check=True):
        if name == "kernel-create-target":
            require(
                argv[:2] == ["docker", "create"]
                and argv[-3:] == [self.image, "--suite", self.suite]
                and argv.count("--entrypoint") == 0
                and argv.count("--name") == 1
                and argv[argv.index("--name") + 1] == self.records["target"]["name"],
                "vma_diagnostic_launch_changed",
            )
            argv = [
                *argv[:-3],
                "--entrypoint",
                "python",
                self.image,
                "-m",
                MODULE,
                "inside",
                "--case",
                self.case,
            ]
        return super().command(name, argv, timeout=timeout, check=check)

    def validate_result(self, result, ready, summary, failure, configuration):
        expected = result_body(self.case, self.manifest)
        require(
            type(result) is dict
            and set(result)
            == set(expected)
            | {"probe", "summary", "strict_failure", "qualification", "kernel_configuration"}
            and all(
                result[key] == value and type(result[key]) is type(value)
                for key, value in expected.items()
                if key != "diagnostic_completed"
            )
            and result["diagnostic_completed"] is (failure is None)
            and canonical(result["qualification"]) == canonical(ready["qualification"])
            and canonical(result["summary"]) == canonical(summary)
            and result["strict_failure"] == failure
            and canonical(result["kernel_configuration"]) == canonical(configuration),
            "vma_diagnostic_result_unbound",
        )
        validate_probe(result["probe"], self.case)
        require(
            canonical(result["probe"]["kernel_configuration"]) == canonical(configuration),
            "vma_diagnostic_config_changed",
        )

    def run(self):
        self.launch("target")
        ready = self.event("target", "READY")
        require(
            set(ready) == {"event", "suite", "cgroup_inode", "source", "qualification"}
            and ready["suite"] == self.suite
            and ready["source"] == {key: self.manifest[key] for key in SOURCE_KEYS}
            and type(ready["cgroup_inode"]) is int
            and 0 < ready["cgroup_inode"] < 2**64,
            "kernel_target_ready_invalid",
        )
        self.target_cgroup = ready["cgroup_inode"]
        validate_qualification(
            ready["qualification"],
            self.target_cgroup,
            self.manifest["files"]["scripts/ac_15_kernel_boundary.py"]["sha256"],
        )
        self.evidence["target"] = ready
        self.launch("collector", self.target_cgroup)
        collector = self.event("collector", "READY")
        validate_ready(collector, self.target_cgroup)
        require(collector["source"] == ready["source"], "kernel_collector_source_mismatch")
        self.evidence["collector"] = collector
        previous = self.snapshot()
        require(not any(previous.values()), "kernel_initial_counters_nonzero")
        configuration = collector["kernel_configuration"]
        self.send("target", {"event": "ATTACHED", "kernel_configuration": configuration})
        begin = self.event("target", "BEGIN")
        require(
            set(begin) == {"event", "phase", "command_sha256", "quiescence"}
            and type(begin["phase"]) is int
            and begin["phase"] == 1
            and begin["command_sha256"] == command_digest(probe_command(self.case)),
            "kernel_phase_inventory_mismatch",
        )
        self.quiescence(begin["quiescence"])
        require(self.snapshot() == previous, "kernel_activity_outside_phase")
        self.send("target", {"event": "BEGIN", "phase": 1})
        end = self.event("target", "END")
        require(
            set(end) == {"event", "phase", "quiescence"}
            and type(end["phase"]) is int
            and end["phase"] == 1,
            "kernel_phase_end_mismatch",
        )
        self.quiescence(end["quiescence"])
        current = self.snapshot()
        summary = phase_summary(previous, current)
        self.evidence["phases"].append(
            {
                "phase": 1,
                "command_sha256": begin["command_sha256"],
                "summary": summary,
                "quiescence": end["quiescence"],
            }
        )
        self.send("target", {"event": "END", "phase": 1, "summary": summary})
        failure = strict_observation(summary, configuration)
        self.evidence["strict_failure"] = failure
        # A complete experiment must observe exactly the one source-fixed call.
        if failure is None:
            validate_one_call(summary)
        if failure is not None:
            original = self.deadline
            self.deadline = min(original, time.monotonic() + 2)
            try:
                result = self.event("target", "RESULT")
            finally:
                self.deadline = original
        else:
            hold = self.event("target", "HOLD")
            require(
                set(hold) == {"event", "phase", "quiescence"}
                and type(hold["phase"]) is int
                and hold["phase"] == 1,
                "kernel_hold_incomplete",
            )
            self.quiescence(hold["quiescence"])
            self.send("collector", "STOP")
            done = self.event("collector", "DONE")
            self.sequence += 1
            require(done.get("sequence") == self.sequence, "kernel_final_sequence_mismatch")
            require(validate_snapshot(done, "DONE") == current, "kernel_final_counters_changed")
            self.evidence["completion"] = done
            self.finish_process("collector")
            self.send("target", {"event": "RELEASE"})
            result = self.event("target", "RESULT")
        self.validate_result(result, ready, summary, failure, configuration)
        self.evidence["result"] = result
        if failure is None:
            self.finish_process("target")
        self.evidence["diagnostic_completed"] = failure is None


def run_diagnostics(runner, image, manifest) -> None:
    remaining_cleanup = float(CLEANUP_SECONDS)
    all_complete = True
    try:
        for case in CASES:
            session = DiagnosticHost(runner, image, manifest, case)
            runner.kernel_sessions.append(session)  # Ownership precedes creation.
            try:
                session.run()
                all_complete = all_complete and session.evidence.get("diagnostic_completed") is True
            except FAILURES as error:
                session.evidence["failure"] = offline.safe_failure_code(error)
                all_complete = False
            finally:
                started = time.monotonic()
                clean = session.cleanup(started + remaining_cleanup)
                remaining_cleanup = max(0, remaining_cleanup - (time.monotonic() - started))
            require(clean["ok"], "vma_diagnostic_cleanup_failed")
    finally:
        runner.report["cleanup"] = {
            "ok": all(
                session.cleaned and session.cleaned["ok"] for session in runner.kernel_sessions
            ),
            "container_count": sum(
                sum(record["attempted"] for record in session.records.values())
                for session in runner.kernel_sessions
            ),
            "images_retained": True,
            "shared_budget_seconds": CLEANUP_SECONDS,
        }
    runner.report["diagnostic_completed"] = all_complete and len(runner.kernel_sessions) == len(
        CASES
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "inside", "probe"))
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--image")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.mode != "run":
        require(
            args.case is not None and args.image is None and args.report is None,
            "vma_diagnostic_arguments",
        )
        if args.mode == "inside":
            return inside(args.case)
        with verification_signals(PROBE_SECONDS):
            result = probe(args.case)
        print(canonical(result).decode(), flush=True)
        return 0
    require(
        args.case is None,
        "vma_diagnostic_arguments",
    )
    if args.report is not None:
        require(
            args.report.is_absolute()
            and not args.report.resolve().is_relative_to(ROOT)
            and not args.report.exists()
            and args.report.parent.is_dir(),
            "report_must_be_new_external_file",
        )
        report_directory = args.report.parent.resolve()
    else:
        report_directory = Path(tempfile.gettempdir()).resolve()
        require(
            report_directory.is_absolute() and not report_directory.is_relative_to(ROOT),
            "report_must_be_external",
        )
    runner = offline.OfflineRun(ROOT)
    runner.report.update(diagnostic_only=True, acceptance=False, diagnostic_completed=False)
    try:
        with (
            verification_signals(HOST_SECONDS * len(CASES) + CLEANUP_SECONDS + 60),
            tempfile.TemporaryDirectory(prefix="ac15-vma-diagnostic-") as temporary,
        ):
            directory = Path(temporary).resolve()
            require(not directory.is_relative_to(ROOT), "temporary_export_inside_source")
            manifest = runner.export(directory)
            runner.docker()
            image = (
                runner.image(args.image, manifest)
                if args.image is not None
                else runner.resolve_image(manifest)
            )
            run_diagnostics(runner, image, manifest)
            require(
                runner.command("unchanged-head", ["git", "rev-parse", "HEAD"])
                .stdout.decode()
                .strip()
                == manifest["source_commit"],
                "caller_head_changed",
            )
            require(
                not runner.command(
                    "unchanged-source", ["git", "status", "--porcelain=v1", "-z"]
                ).stdout,
                "caller_worktree_changed",
            )
    except FAILURES as error:
        runner.report.update(diagnostic_completed=False, failure=offline.safe_failure_code(error))
    # No paths mark ok/acceptance true: exit zero means diagnostic collection only.
    output = json.dumps(runner.report, sort_keys=True, indent=2) + "\n"
    if args.report is not None:
        with args.report.open("x") as stream:
            stream.write(output)
        report_path = args.report
    else:
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="marketing-agents-ac18-vma-",
            suffix=".json",
            dir=report_directory,
            delete=False,
        ) as stream:
            stream.write(output)
            report_path = Path(stream.name)
    print(output, end="")
    print("AC18 diagnostic report: " + str(report_path))
    return 0 if runner.report["diagnostic_completed"] and runner.report["cleanup"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
