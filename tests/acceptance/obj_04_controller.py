"""OBJ-04 run the production supervisor with a test-only Python Popen preloader.

No settings, runtime, worker, environment, readiness, or supervisor method is
replaced. Native child argv gains only instrumentation before its original -m
module. Vite is launched unchanged; its Node networking is not instrumented here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import dev  # noqa: E402
from tests.acceptance.obj_04_guard import GuardRecorder, assert_clean_environment  # noqa: E402


def main() -> int:
    report_root = Path(sys.argv[1])
    assert report_root.is_dir()
    recorder = GuardRecorder(report_root / "controller.json", "controller")
    recorder.install()
    original_popen = subprocess.Popen
    sequence = 0

    def instrumented_popen(command, *args, **kwargs):
        nonlocal sequence
        command = [str(part) for part in command]
        runtime = len(command) >= 3 and command[1] == "-m"
        is_version = command[-1:] == ["--version"]
        is_web = command[1:3] == ["exec", "vite"]
        assert runtime or is_version or is_web, "unexpected native launch command"
        environment = kwargs.get("env")
        assert isinstance(environment, dict), "native launch inherited the controller environment"
        assert_clean_environment(environment, runtime=runtime or is_web)
        original_command = command
        sequence += 1
        if runtime:
            assert command[2].startswith("marketing_agents.workers.")
            command = [
                command[0],
                str(ROOT / "tests/acceptance/obj_04_child.py"),
                str(report_root / f"python-{sequence}.json"),
                *command[2:],
            ]
        # Crucially pass kwargs through unchanged, including the actual env
        # object, cwd, stdout/stderr, and start_new_session ownership boundary.
        process = original_popen(command, *args, **kwargs)
        event = {
            "pid": process.pid,
            "role": original_command[2] if runtime else "version" if is_version else "web",
            "owned_group": kwargs.get("start_new_session", False),
            "environment_names": sorted(environment),
            "environment_verified": True,
        }
        with (report_root / "launches.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
        return process

    subprocess.Popen = instrumented_popen
    previous_trace = sys.gettrace()

    def trace_native_errors(frame, event, argument):
        if frame.f_code.co_filename != str(ROOT / "scripts/dev.py"):
            return None
        if event == "exception":
            error = argument[1]
            category = {
                "function": frame.f_code.co_name,
                "line": frame.f_lineno,
                "type": type(error).__name__,
                "errno": error.errno if isinstance(error, OSError) else None,
            }
            errors = recorder.data.setdefault("native_exceptions", [])
            errors.append(category)
            del errors[:-32]
            if isinstance(error, OSError):
                os_errors = recorder.data.setdefault("native_os_errors", [])
                os_errors.append(category)
                del os_errors[:-32]
            recorder.save()
        return trace_native_errors

    # The production entry point deliberately redacts OSError details. Keep
    # only categorical test diagnostics; never serialize exception text/args.
    sys.settrace(trace_native_errors)
    try:
        return dev.main(sys.argv[2:])
    finally:
        sys.settrace(previous_trace)
        subprocess.Popen = original_popen
        recorder.finish()


if __name__ == "__main__":
    raise SystemExit(main())
