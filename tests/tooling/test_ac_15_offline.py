"""AC-15 runner authority, complete inventories and fail-closed syscall accounting."""

from __future__ import annotations

import hashlib
import json
import signal
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import ac_15_offline as offline
from scripts.del_05_clean_state import VerificationFailure

IMAGE = "sha256:" + "a" * 64
COMMIT = "b" * 40
TREE = "c" * 40
MANIFEST = {"source_commit": COMMIT, "source_tree": TREE, "source_sha256": "d" * 64}


@pytest.fixture
def isolated_inside(tmp_path, monkeypatch):
    """Exercise the runner without touching Docker, real sockets or source state."""
    for key, value in offline.SAFE_ENV.items():
        monkeypatch.setenv(key, value)
    for key, value in MANIFEST.items():
        monkeypatch.setenv("AC15_" + key.upper(), value)
    read_text = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            json.dumps(MANIFEST)
            if path == Path("/opt/ac15/source-manifest.json")
            else read_text(path, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(offline, "ROOT", tmp_path)
    monkeypatch.setattr(offline.os, "getuid", lambda: 10001)
    monkeypatch.setattr(offline, "verify_source", lambda *args: None)
    monkeypatch.setattr(offline, "kernel_canaries", lambda: {})
    monkeypatch.setattr(offline, "install_browser_transport", lambda: {})
    monkeypatch.setattr(offline, "prove_browser_transport", lambda collect: {})
    monkeypatch.setattr(offline, "prove_trace_guard", lambda: {})


def test_ac_15_preflight_consumes_original_command_budget(isolated_inside, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(offline.time, "monotonic", lambda: clock[0])

    def preflight():
        clock[0] += 23
        return {}

    def command(argv, *, timeout):
        assert timeout == offline.SUITE_TIMEOUT - 23
        raise subprocess.TimeoutExpired(argv, timeout, output=b"private output")

    monkeypatch.setattr(offline, "kernel_canaries", preflight)
    monkeypatch.setattr(offline, "traced_command", command)
    result = offline.inside("backend")
    assert result["failure"] == "TimeoutExpired"
    assert result["failed_command"]["output_complete"] is False


def test_ac_15_exhausted_shared_budget_keeps_first_command(isolated_inside, monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(offline.time, "monotonic", lambda: clock[0])

    def command(argv, *, timeout):
        calls.append(argv)
        assert timeout == offline.SUITE_TIMEOUT
        clock[0] += timeout + 1
        return completed(argv), {"rejected": 0}

    monkeypatch.setattr(offline, "traced_command", command)
    result = offline.inside("browser")
    assert calls == [offline.SUITES["browser"][0]]
    assert len(result["commands"]) == 1
    assert result["failure"] == "suite_deadline_exceeded"
    assert result["failed_command"]["argv"] == list(offline.SUITES["browser"][1])
    assert result["ok"] is False


def test_ac_15_whole_container_alarm_covers_preflight(isolated_inside, monkeypatch, capsys):
    def preflight():
        remaining, _ = signal.getitimer(signal.ITIMER_REAL)
        assert 0 < remaining <= offline.CONTAINER_TIMEOUT
        signal.raise_signal(signal.SIGALRM)

    monkeypatch.setattr(offline, "kernel_canaries", preflight)
    monkeypatch.setattr(offline, "traced_command", lambda *a, **k: pytest.fail("child started"))
    assert offline.main(["inside", "--suite", "backend"]) == 1
    output = capsys.readouterr().out
    assert output.count(offline.PREFIX) == 1
    result = json.loads(output.removeprefix(offline.PREFIX))
    assert result["failure"] == "verification_execution_deadline"
    assert not result["commands"] and not result["ok"]
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_ac_15_whole_container_alarm_covers_trace_parse_and_keeps_child(monkeypatch):
    child = completed(["synthetic"], b"private completed output")
    monkeypatch.setattr(offline.subprocess, "run", lambda *args, **kwargs: child)

    def parse(path):
        signal.raise_signal(signal.SIGALRM)

    monkeypatch.setattr(offline, "trace_file_summary", parse)
    with (
        offline.verification_signals(offline.CONTAINER_TIMEOUT),
        pytest.raises(
            offline.TracedCommandFailure, match="verification_execution_deadline"
        ) as error,
    ):
        offline.collect_trace(("synthetic",), timeout=offline.SUITE_TIMEOUT)
    assert error.value.result is child
    evidence = offline.failed_command_evidence(
        ("synthetic",), elapsed=1, error=error.value, suite="native"
    )
    assert evidence["exit_code"] == 0 and evidence["output_complete"] is True
    assert evidence["stdout_sha256"] == hashlib.sha256(child.stdout).hexdigest()
    assert "private" not in json.dumps(evidence)


@pytest.mark.parametrize("phase", ["projection", "final-source"])
def test_ac_15_post_command_alarm_keeps_captured_evidence(isolated_inside, monkeypatch, phase):
    output = b"private successful child output"
    monkeypatch.setattr(
        offline,
        "traced_command",
        lambda command, **kwargs: (completed(command, output), {"rejected": 0}),
    )
    if phase == "projection":
        monkeypatch.setattr(
            offline, "project_diagnostics", lambda *args: signal.raise_signal(signal.SIGALRM)
        )
    else:
        monkeypatch.setattr(
            offline,
            "project_diagnostics",
            lambda *args: {"pytest_summary": {"passed": 1, "failed": 0, "collection_errors": 0}},
        )
        checks = []

        def source(*args):
            checks.append(True)
            if len(checks) == 2:
                signal.raise_signal(signal.SIGALRM)

        monkeypatch.setattr(offline, "verify_source", source)
    result = offline.inside_result("backend")
    assert not result["ok"] and result["failure"] == "verification_execution_deadline"
    assert len(result["commands"]) == 1
    assert result["commands"][0]["stdout_sha256"] == hashlib.sha256(output).hexdigest()
    assert result["commands"][0]["network_syscalls"]["rejected"] == 0
    assert "private" not in json.dumps(result)


def test_ac_15_host_alarm_leaves_owned_cleanup_and_report_time(tmp_path, monkeypatch):
    runner = offline.OfflineRun(tmp_path)
    cleanup = []
    monkeypatch.setattr(offline, "OfflineRun", lambda root: runner)
    monkeypatch.setattr(runner, "export", lambda directory: MANIFEST)

    def docker():
        signal.raise_signal(signal.SIGALRM)

    def clean():
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
        cleanup.append(True)
        runner.report["cleanup"] = {"ok": True}

    monkeypatch.setattr(runner, "docker", docker)
    monkeypatch.setattr(runner, "cleanup", clean)
    report = tmp_path / "failed-report.json"
    assert offline.main(["run", "--suite", "backend", "--report", str(report)]) == 1
    assert cleanup == [True]
    result = json.loads(report.read_text())
    assert result["failure"] == "verification_execution_deadline"
    assert result["cleanup"]["ok"] and not result["ok"]
    manifest = json.loads((offline.ROOT / "docs/verification/requirements/AC-15.json").read_text())
    bounds = [
        gate["timeout_seconds"] for gate in manifest["gates"] if "complete-offline" in gate["id"]
    ]
    assert bounds == [1800] * 4
    assert offline.SUITE_TIMEOUT == 1700
    assert offline.CONTAINER_TIMEOUT == 1720
    assert offline.HOST_TIMEOUT + 2 * 15 + 30 == 1800


def completed(args, stdout=b"", code=0):
    return subprocess.CompletedProcess(args, code, stdout, b"")


def inspected(name, owner, image=IMAGE):
    return {
        "Id": hashlib.sha256(name.encode()).hexdigest(),
        "Name": "/" + name,
        "Image": image,
        "Config": {"User": "10001:10001", "Labels": {offline.LABEL + "owner": owner}},
        "HostConfig": {
            "Init": True,
            "NetworkMode": "none",
            "PortBindings": {},
            "Binds": [],
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
        },
        "Mounts": [],
        "NetworkSettings": {"Networks": {"none": {}}},
    }


@pytest.mark.parametrize(
    "environment",
    [
        {"LLM_PROVIDER": "real"},
        {"connector_mode": "REAL"},
        {"allow_external_network": "yes"},
        {"REAL_LLM_OPT_IN": "1"},
        {"REAL_CONNECTOR_OPT_IN": " true "},
        {"App_Env": "PRODUCTION"},
        {"AUTH_MODE": "session"},
        {"LLM_PROVIDER": "mock", "llm_provider": "real"},
    ],
)
def test_ac_15_rejects_unsafe_case_insensitive_caller_environment(environment):
    with pytest.raises(VerificationFailure, match="unsafe_caller_runtime"):
        offline.validate_caller_environment(environment)


def test_ac_15_false_optins_are_normalized_and_secrets_not_retained(tmp_path, monkeypatch):
    offline.validate_caller_environment({"ALLOW_EXTERNAL_NETWORK": " OFF ", "APP_ENV": "test"})
    monkeypatch.setenv("PRIVATE_TOKEN", "private-canary")
    monkeypatch.setenv("HTTP_PROXY", "http://private.invalid")
    runner = offline.OfflineRun(tmp_path)
    assert "PRIVATE_TOKEN" not in runner.environment and "HTTP_PROXY" not in runner.environment
    assert "private-canary" not in json.dumps(runner.report)


def test_ac_15_source_manifest_detects_content_and_mode_drift(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("original")
    path.chmod(0o644)
    manifest = offline.source_manifest(tmp_path, COMMIT, TREE)
    offline.verify_source(tmp_path, manifest)
    path.write_text("changed")
    with pytest.raises(VerificationFailure, match="source_content_changed"):
        offline.verify_source(tmp_path, manifest)
    path.write_text("original")
    path.chmod(0o755)
    with pytest.raises(VerificationFailure, match="source_content_changed"):
        offline.verify_source(tmp_path, manifest)


def test_ac_15_fingerprint_preserves_component_order_after_manifest_json_roundtrip(tmp_path):
    names = ("nested/file.py", "nested-file.py", "nested.file.py", "nested_2.py")
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
    assert sorted(names) != sorted(names, key=Path)
    manifest = offline.source_manifest(tmp_path, COMMIT, TREE)
    serialized = json.dumps(manifest, sort_keys=True)
    offline.verify_source(tmp_path, json.loads(serialized))


def test_ac_15_dirty_worktree_refused_before_archive_or_docker(tmp_path, monkeypatch):
    runner = offline.OfflineRun(tmp_path)
    calls = []

    def command(name, args, **kwargs):
        calls.append(args)
        return completed(args, b" M source.py\0")

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(VerificationFailure, match="clean_worktree"):
        runner.export(tmp_path)
    assert calls == [["git", "status", "--porcelain=v1", "-z"]]


def test_ac_15_image_requires_all_exact_source_labels_and_nonroot():
    image = {
        "Id": IMAGE,
        "Config": {
            "User": "10001:10001",
            "Labels": {
                offline.LABEL + "schema": "1",
                **{offline.LABEL + key.replace("_", "-"): value for key, value in MANIFEST.items()},
            },
        },
    }
    assert offline.validate_image(image, MANIFEST) == IMAGE
    for field in MANIFEST:
        wrong = deepcopy(image)
        wrong["Config"]["Labels"][offline.LABEL + field.replace("_", "-")] = "wrong"
        with pytest.raises(VerificationFailure, match="image_source_identity"):
            offline.validate_image(wrong, MANIFEST)
    image["Config"]["User"] = "0"
    with pytest.raises(VerificationFailure, match="image_user"):
        offline.validate_image(image, MANIFEST)


@pytest.mark.parametrize("identities", [b"", (IMAGE + "\nsha256:" + "f" * 64).encode()])
def test_ac_15_missing_or_ambiguous_preacquired_image_never_builds(
    tmp_path, monkeypatch, identities
):
    runner = offline.OfflineRun(tmp_path)
    calls = []

    def command(name, args, **kwargs):
        calls.append(args)
        return completed(args, identities)

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(VerificationFailure, match="exactly_one_preacquired"):
        runner.resolve_image(MANIFEST)
    assert len(calls) == 1 and calls[0][:5] == ["docker", "image", "ls", "--quiet", "--no-trunc"]
    assert "build" not in calls[0] and "pull" not in calls[0]


def test_ac_15_repeated_tags_resolve_one_immutable_image(tmp_path, monkeypatch):
    runner = offline.OfflineRun(tmp_path)
    monkeypatch.setattr(
        runner, "command", lambda *a, **k: completed(a, (IMAGE + "\n" + IMAGE).encode())
    )
    verified = []
    monkeypatch.setattr(
        runner,
        "image",
        lambda identity, manifest: verified.append((identity, manifest)) or identity,
    )
    assert runner.resolve_image(MANIFEST) == IMAGE
    assert verified == [(IMAGE, MANIFEST)]


@pytest.mark.parametrize(
    "change",
    [
        {"HostConfig": {"NetworkMode": "host"}},
        {"HostConfig": {"Init": False}},
        {"HostConfig": {"Init": None}},
        {"HostConfig": {"Init": 1}},
        {"Mounts": [{"Source": "/host"}]},
        {"Config": {"User": "0"}},
        {"HostConfig": {"Privileged": True}},
        {"HostConfig": {"CapDrop": []}},
        {"HostConfig": {"SecurityOpt": []}},
        {"NetworkSettings": {"Networks": {"bridge": {}}}},
    ],
)
def test_ac_15_container_boundaries_fail_closed(change):
    value = inspected("owned", "owner")
    for key, replacement in change.items():
        if isinstance(replacement, dict):
            value[key].update(replacement)
        else:
            value[key] = replacement
    with pytest.raises(VerificationFailure):
        offline.validate_container(value, name="owned", owner="owner", image=IMAGE)


def test_ac_15_full_fixed_suites_only_create_network_none_and_remove_exact_owned_ids(
    tmp_path, monkeypatch
):
    runner = offline.OfflineRun(tmp_path)
    calls = []
    containers = {}
    monkeypatch.setattr(runner, "image", lambda identity, manifest: identity)
    monkeypatch.setattr(runner, "inspect_container", lambda name: containers[name])

    def command(name, args, **kwargs):
        calls.append(args)
        if name.startswith("create-"):
            container_name = args[args.index("--name") + 1]
            value = containers[container_name] = inspected(container_name, runner.owner)
            return completed(args, value["Id"].encode())
        if name.startswith("run-"):
            suite = name.removeprefix("run-")
            evidence = {
                **MANIFEST,
                "suite": suite,
                "ok": True,
                "commands": [{"argv": list(cmd)} for cmd in offline.SUITES[suite]],
            }
            return completed(args, (offline.PREFIX + json.dumps(evidence)).encode())
        return completed(args)

    monkeypatch.setattr(runner, "command", command)
    runner.execute(IMAGE, MANIFEST)
    assert [entry["suite"] for entry in runner.report["suites"]] == list(offline.SUITES)
    assert len(containers) == 4
    for command in calls:
        assert "build" not in command and "pull" not in command
        if command[1] == "create":
            assert command[command.index("--network") + 1] == "none"
            assert "--pull=never" in command and "--cap-drop=ALL" in command
            assert "--init" in command
            assert "RAYON_NUM_THREADS=1" in command
            assert (
                "--volume" not in command
                and "--mount" not in command
                and "--publish" not in command
            )
            assert IMAGE in command
    runner.cleanup()
    removals = [command for command in calls if command[1:3] == ["container", "rm"]]
    assert {command[-1] for command in removals} == {value["Id"] for value in containers.values()}
    assert all(command[:-1] == ["docker", "container", "rm", "--force"] for command in removals)
    assert runner.report["cleanup"]["ok"]


def test_ac_15_cleanup_refuses_foreign_label_without_removing(tmp_path, monkeypatch):
    runner = offline.OfflineRun(tmp_path)
    runner.containers.append(("owned", IMAGE))
    monkeypatch.setattr(runner, "inspect_container", lambda name: inspected(name, "foreign"))
    monkeypatch.setattr(runner, "command", lambda *a, **k: pytest.fail("foreign cleanup attempted"))
    runner.cleanup()
    assert runner.report["cleanup"]["ok"] is False


@pytest.mark.parametrize(
    "line",
    [
        "42 connect(3, {sa_family=AF_INET, sin_port=htons(9), "
        'sin_addr=inet_addr("192.0.2.1")}, 16) = -1 ENETUNREACH',
        '42 sendto(3, ""..., 9, 0, {sa_family=AF_INET, sin_port=htons(53), '
        'sin_addr=inet_addr("192.0.2.2")}, 16) = -1 ENETUNREACH',
        "42 sendmsg(3, {msg_name={sa_family=AF_INET6, "
        'inet_pton(AF_INET6, "2001:db8::1", &sin6_addr)}, msg_namelen=28}, 0) = -1 ENETUNREACH',
        "42 sendmmsg(3, [{msg_hdr={msg_name={sa_family=AF_INET, "
        'sin_addr=inet_addr("192.0.2.1")}}}], 1, 0) = -1 ENETUNREACH',
    ],
)
def test_ac_15_syscall_audit_rejects_external_even_when_kernel_denied(line):
    summary = offline.trace_summary(line.encode())
    assert summary["rejected"] == 1 and summary["loopback"] == 0
    assert "192.0.2" not in json.dumps(summary) and "2001:db8" not in json.dumps(summary)


def test_ac_15_trace_allows_owned_loopback_unix_and_connected_sends():
    trace = "\n".join(
        [
            '42 connect(3, {sa_family=AF_INET, sin_addr=inet_addr("127.0.0.1")}, 16) = 0',
            '42 connect(3, {sa_family=AF_INET6, inet_pton(AF_INET6, "::1", &sin6_addr)}, 28) = 0',
            "42 connect(3, {sa_family=AF_INET6, "
            'inet_pton(AF_INET6, "::ffff:127.0.0.1", &sin6_addr)}, 28) = 0',
            '42 connect(3, {sa_family=AF_UNIX, sun_path=""...}, 16) = 0',
            '42 sendto(3, ""..., 100, MSG_NOSIGNAL, NULL, 0) = 100',
            "42 sendmsg(3, {msg_name=NULL, msg_namelen=0}, 0) = 0",
            '42 connect(4, {sa_family=AF_UNIX, sun_path=""...}, 16 <unfinished ...>',
            "42 <... connect resumed>) = 0",
        ]
    )
    summary = offline.trace_summary(trace.encode())
    assert summary["loopback"] == 3 and summary["unix"] == 2
    assert summary["connected_send"] == 2 and summary["rejected"] == 0


@pytest.mark.parametrize(
    "trace",
    [
        b"unclassified output",
        b"86 ???( <detached ...>",
        b"117 ???( <unfinished ...>",
        b"connect(3, 0x1234, 16) = -1 EFAULT",
        b"sendto(3, 0x1234, 16, 0, 0x4321, 16) = -1",
    ],
)
def test_ac_15_unknown_trace_destination_cannot_pass(trace):
    with pytest.raises(VerificationFailure):
        offline.trace_summary(trace)


def test_ac_15_traced_child_zero_exit_cannot_swallow_external_attempt(tmp_path, monkeypatch):
    traces = []

    def run(command, **kwargs):
        trace = Path(command[command.index("-o") + 1])
        traces.append(trace)
        assert trace.stat().st_mode & 0o777 == 0o600
        assert command[:4] == ["strace", "--seccomp-bpf", "-f", "-qq"]
        assert command[command.index("-e") + 1] == (
            "trace=connect,sendto,sendmsg,sendmmsg,seccomp,prctl"
        )
        trace.write_text(
            "3 connect(7, {sa_family=AF_INET, "
            'sin_addr=inet_addr("192.0.2.1")}, 16) = -1 ENETUNREACH\n'
        )
        return completed(command)

    monkeypatch.setattr(offline.subprocess, "run", run)
    with pytest.raises(VerificationFailure, match="external_network_syscall_observed"):
        offline.traced_command(("python", "child.py"), timeout=1)
    assert traces and all(not path.exists() for path in traces)


def test_ac_15_kernel_probe_never_runs_before_loopback_only_proof(tmp_path, monkeypatch):
    sysfs, ipv4, ipv6 = namespace_fixture(tmp_path)
    interface(sysfs, "eth0", "0x1003", "up")
    original = offline.kernel_namespace
    monkeypatch.setattr(offline, "kernel_namespace", lambda: original(sysfs, ipv4, ipv6))
    monkeypatch.setattr(
        offline.subprocess, "run", lambda *a, **k: pytest.fail("unsafe canary execution")
    )
    with pytest.raises(VerificationFailure, match="nonloopback"):
        offline.kernel_canaries()


def test_ac_15_kernel_probe_uses_unguarded_children_only_after_no_route_proof(monkeypatch):
    monkeypatch.setattr(
        offline, "kernel_namespace", lambda: {"interfaces": ["lo"], "ipv4_route_count": 0}
    )
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return completed(
            command, b'{"errno":101}' if len(calls) == 1 else b'{"code":"ENETUNREACH"}'
        )

    monkeypatch.setattr(offline.subprocess, "run", run)
    proof = offline.kernel_canaries()
    assert proof["interfaces"] == ["lo"] and proof["ipv4_route_count"] == 0
    assert calls[0][0][1:4] == ["-I", "-S", "-c"]
    assert calls[1][0][:2] == ["node", "-e"]
    assert all("192.0.2.1" in command[-1] for command, _ in calls)
    assert all(set(options["env"]) <= {"PATH", "HOME"} for _, options in calls)


@pytest.mark.parametrize(
    ("output", "kind", "count"),
    [
        (b" Tests  52 passed (52)\n", "vitest", 52),
        (b"# pass 19\n", "node", 19),
        ("\u2139 tests 4\n\u2139 suites 0\n\u2139 pass 4\n\u2139 fail 0\n".encode(), "node", 4),
        (b"2 passed in 12.4s\n", "pytest", 2),
        (b"  8 passed (10s)\n  9 passed (9s)\n", "playwright", 17),
    ],
)
def test_ac_15_reports_nonzero_safe_test_counts(output, kind, count):
    assert offline.test_summary(output, kind)["passed"] == count


def test_ac_15_zero_tests_or_missing_summary_fails():
    for output in (b"", b"0 passed", b"Tests 0 passed (0)"):
        with pytest.raises(VerificationFailure):
            offline.test_summary(output, "pytest")


def test_ac_15_inventory_and_dockerfile_preserve_full_scope_and_separate_acquisition():
    assert tuple(offline.SUITES) == ("backend", "frontend", "browser", "native")
    assert offline.SUITES["backend"] == (("make", "verify-del-05-offline-backend"),)
    assert offline.SUITES["native"] == (("make", "verify-native-offline"),)
    assert offline.SUITES["browser"][-1] == ("node", "apps/web/scripts/run-web-e2e.mjs")
    assert list(offline.SUITES["frontend"][1][2:]) == sorted(
        str(path.relative_to(offline.ROOT))
        for path in (offline.ROOT / "tests/network").glob("*.test.mjs")
    )
    dockerfile = (offline.ROOT / "docker/ac-15.Dockerfile").read_text()
    assert dockerfile.count("@sha256:") == 3
    assert "uv sync --frozen --no-install-project" in dockerfile
    assert "pnpm install --frozen-lockfile" in dockerfile
    assert "PLAYWRIGHT_BROWSERS_PATH=0 pnpm --dir apps/web exec playwright install" in dockerfile
    assert "strace" in dockerfile and "USER 10001:10001" in dockerfile


def test_ac_15_fixed_workers_preserve_every_backend_stage_and_full_pytest():
    assert offline.SAFE_ENV["RAYON_NUM_THREADS"] == "1"
    dockerfile = (offline.ROOT / "docker/ac-15.Dockerfile").read_text()
    assert "CI=true RAYON_NUM_THREADS=1" in dockerfile
    recipe = (
        (offline.ROOT / "Makefile")
        .read_text()
        .split("verify-del-05-offline-backend:\n", 1)[1]
        .split("\n\n", 1)[0]
        .splitlines()
    )
    assert [line.split()[3] for line in recipe] == [
        "ruff-format-source",
        "ruff-check-source",
        "ruff-format-tooling",
        "ruff-check-tooling",
        "mypy",
        "architecture",
        "catalog",
        "pytest",
    ]
    assert recipe[-1].strip() == (
        "python -m scripts.del_05_offline_diagnostics pytest -- "
        "python -m pytest -q -n 2 --dist=loadfile --max-worker-restart=0 "
        "-p scripts.del_05_offline_diagnostics"
    )
    assert offline.SUITES["backend"] == (("make", "verify-del-05-offline-backend"),)


@pytest.mark.parametrize("threads", [None, "", "2"])
def test_ac_15_inside_requires_fixed_ruff_worker_environment(monkeypatch, threads):
    for key, value in offline.SAFE_ENV.items():
        monkeypatch.setenv(key, value)
    if threads is None:
        monkeypatch.delenv("RAYON_NUM_THREADS")
    else:
        monkeypatch.setenv("RAYON_NUM_THREADS", threads)
    with pytest.raises(VerificationFailure, match="unsafe_container_environment"):
        offline.inside("backend")


def test_ac_15_project_is_installed_after_source_copy_during_acquisition_only():
    lines = (offline.ROOT / "docker/ac-15.Dockerfile").read_text().splitlines()
    dependency_install = lines.index(
        "RUN uv sync --frozen --no-install-project --python /usr/local/bin/python3"
    )
    source_copy = lines.index("COPY source/ /app/")
    project_install = lines.index("RUN uv sync --frozen --python /usr/local/bin/python3")
    ownership = lines.index("RUN chown -R 10001:10001 /app /opt/corepack")
    assert dependency_install < source_copy < project_install < ownership
    assert [line for line in lines if "uv sync" in line] == [
        lines[dependency_install],
        lines[project_install],
    ]
    assert 'ENTRYPOINT ["python", "scripts/ac_15_offline.py", "inside"]' in lines
    assert "UV_OFFLINE=1 UV_NO_SYNC=1 UV_FROZEN=1" in "\n".join(lines)


@pytest.mark.parametrize(
    "extra",
    [
        "apps/api/src/new.py",
        "tests/new_test.py",
        "scripts/new.py",
        "catalog/new.json",
        "config/new.json",
        "__pycache__/new.py",
    ],
)
def test_ac_15_new_execution_input_is_rejected(tmp_path, extra):
    (tmp_path / "tracked").write_text("original")
    manifest = offline.source_manifest(tmp_path, COMMIT, TREE)
    path = tmp_path / extra
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not committed")
    with pytest.raises(VerificationFailure, match="untracked_execution_input"):
        offline.verify_source(tmp_path, manifest)


def test_ac_15_new_symlink_rejected_and_known_generated_output_allowed(tmp_path):
    (tmp_path / "tracked").write_text("original")
    manifest = offline.source_manifest(tmp_path, COMMIT, TREE)
    generated = tmp_path / "apps/web/dist"
    generated.mkdir(parents=True)
    (generated / "bundle.js").write_text("generated")
    offline.verify_source(tmp_path, manifest)
    link = tmp_path / "new.py"
    link.symlink_to(tmp_path / "tracked")
    with pytest.raises(VerificationFailure, match="untracked_source_symlink"):
        offline.verify_source(tmp_path, manifest)


def test_ac_15_trace_payload_cannot_impersonate_a_destination_and_netlink_is_kernel_only():
    line = '1 sendto(3, "sa_family=AF_INET AF_UNIX private", 35, 0, NULL, 0) = 35'
    assert offline.trace_summary(line.encode())["connected_send"] == 1
    line = "2 connect(5, {sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}, 12) = 0"
    assert offline.trace_summary(line.encode())["kernel_ipc"] == 1
    with pytest.raises(VerificationFailure, match="kernel_destination"):
        offline.trace_summary(line.replace("nl_pid=0", "nl_pid=42").encode())


@pytest.mark.parametrize("other", ["0x1234", "{sa_family=AF_IPX}", "{sa_family=AF_INET}"])
def test_ac_15_mixed_batched_unknown_destination_fails(other):
    trace = (
        "1 sendmmsg(3, [{msg_hdr={msg_name={sa_family=AF_INET, "
        'sin_addr=inet_addr("127.0.0.1")}}}, {msg_hdr={msg_name=' + other + "}}], 2, 0) = -1"
    )
    with pytest.raises(VerificationFailure):
        offline.trace_summary(trace.encode())


def test_ac_15_mixed_batched_external_destination_is_not_hidden():
    trace = (
        "1 sendmmsg(3, [{msg_hdr={msg_name={sa_family=AF_INET, "
        'sin_addr=inet_addr("127.0.0.1")}}}, {msg_hdr={msg_name={sa_family=AF_INET, '
        'sin_addr=inet_addr("192.0.2.1")}}}], 2, 0) = -1'
    )
    assert offline.trace_summary(trace.encode())["rejected"] == 1


def test_ac_15_resumed_arguments_are_reassembled_per_pid_before_classification():
    trace = "\n".join(
        [
            "42 sendmmsg(3, <unfinished ...>",
            "43 connect(5, <unfinished ...>",
            "<... sendmsg resumed>{msg_name=NULL}, 0) = 1",
        ]
    )
    with pytest.raises(VerificationFailure, match="orphan_resume"):
        offline.trace_summary(trace.encode())
    trace = "\n".join(
        [
            "42 sendmmsg(3, <unfinished ...>",
            "43 connect(5, <unfinished ...>",
            "[pid 43] <... connect resumed>{sa_family=AF_INET, "
            'sin_addr=inet_addr("127.0.0.1")}, 16) = 0',
            "42 <... sendmmsg resumed>[{msg_hdr={msg_name=NULL, msg_namelen=0}, msg_len=1}, "
            '{msg_hdr={msg_name={sa_family=AF_INET, sin_addr=inet_addr("192.0.2.1")}}, '
            "msg_len=1}], 2, 0) = 2",
        ]
    )
    summary = offline.trace_summary(trace.encode())
    assert summary["loopback"] == 1 and summary["rejected"] == 1


@pytest.mark.parametrize(
    "trace",
    [
        "42 <... connect resumed>) = 0",
        "42 connect(3, <unfinished ...>\n43 <... connect resumed>) = 0",
        "42 connect(3, <unfinished ...>\n42 <... sendto resumed>) = 0",
        "42 connect(3, <unfinished ...>",
        "42 connect(3, <unfinished ...>\n42 sendmsg(3, {msg_name=NULL}, 0) = 0",
        "42 connect(3, <unfinished ...>\n42 +++ killed by SIGKILL +++",
        "42 connect(3, <unfinished ...>\n42 <... connect resumed><detached ...>",
        "42 <... invented resumed>) = 0",
        "unexpected --- arbitrary marker",
        "strace: Process 42 detached",
        "42 +++ arbitrary lifecycle marker +++",
    ],
)
def test_ac_15_incomplete_or_unmatched_records_never_pass(trace):
    with pytest.raises(VerificationFailure):
        offline.trace_summary(trace.encode())


def test_ac_15_only_narrow_lifecycle_records_are_ignored():
    trace = "\n".join(
        [
            "42 --- SIGCHLD {si_signo=SIGCHLD, si_code=CLD_EXITED, si_pid=43} ---",
            "43 +++ exited with 0 +++",
            "44 +++ killed by SIGTERM +++",
            "45 +++ killed by SIGSEGV (core dumped) +++",
            "46 +++ superseded by execve in pid 47 +++",
        ]
    )
    summary = offline.trace_summary(trace.encode())
    assert summary["loopback"] == summary["unix"] == summary["rejected"] == 0


@pytest.mark.parametrize(
    "tail",
    [
        "0x1234",
        "...",
        "... /* 0x1234 */",
        "{msg_hdr=0x1234, msg_len=1}",
        "{msg_hdr={msg_name=NULL, ...}, msg_len=1}",
        "{msg_hdr={msg_namelen=16}, msg_len=1}",
        "{msg_hdr={msg_name=NULL}, ...}",
    ],
)
def test_ac_15_opaque_or_truncated_whole_batch_elements_fail(tail):
    trace = "1 sendmmsg(3, [{msg_hdr={msg_name=NULL}, msg_len=1}, " + tail + "], 2, 0) = -1 EFAULT"
    with pytest.raises(VerificationFailure, match="vector_unparsed"):
        offline.trace_summary(trace.encode())


def test_ac_15_batch_length_must_match_declared_count():
    trace = "1 sendmmsg(3, [{msg_hdr={msg_name=NULL}, msg_len=1}], 2, 0) = 1"
    with pytest.raises(VerificationFailure, match="vector_unparsed"):
        offline.trace_summary(trace.encode())
    summary = offline.trace_summary(trace.replace("], 2,", "], 1,").encode())
    assert summary["connected_send"] == 1 and summary["rejected"] == 0


def test_ac_15_trace_canaries_require_observed_negative_and_positive_child(monkeypatch):
    calls = []

    def collect(command, **kwargs):
        calls.append(command)
        summary = {
            "rejected": int(len(calls) != 2),
            "unix": int(len(calls) == 2),
            "filter_changes": int(len(calls) == 4),
        }
        output = (
            b'{"filter_installed": true, "masked_connect_errno": 1}' if len(calls) == 4 else b""
        )
        return completed(command, output), summary

    monkeypatch.setattr(offline, "collect_trace", collect)
    assert offline.prove_trace_guard() == {
        "swallowed_child_syscall_rejected": True,
        "unix_child_trace_observed": True,
        "swallowed_grandchild_syscall_rejected": True,
        "masking_filter_install_rejected": True,
    }
    assert len(calls) == 4 and all(command[1:4] == ("-I", "-S", "-c") for command in calls)
    assert "subprocess.run" in calls[2][-1] and "192.0.2.1" in calls[2][-1]
    assert calls[3][-1] == offline.SECCOMP_MASK_CANARY
    assert '{"aarch64": (277, 203), "x86_64": (317, 42)}' in calls[3][-1]
    monkeypatch.setattr(
        offline, "collect_trace", lambda cmd, **kw: (completed(cmd), {"rejected": 0, "unix": 0})
    )
    with pytest.raises(VerificationFailure, match="canary_not_rejected"):
        offline.prove_trace_guard()


@pytest.mark.parametrize(
    "call",
    [
        "seccomp(SECCOMP_SET_MODE_FILTER, 0, {len=4, filter=0x1234}) = 0",
        "seccomp(SECCOMP_SET_MODE_FILTER, 0, 0x1234) = -1 EFAULT",
        "seccomp(SECCOMP_SET_MODE_STRICT, 0, NULL) = -1 EINVAL",
        "prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, 0x1234) = 0",
        "prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, NULL) = -1 EFAULT",
        "prctl(PR_SET_SECCOMP, opaque) = -1 EINVAL",
    ],
)
def test_ac_15_descendant_filter_attempt_always_rejected(call):
    summary = offline.trace_summary(("42 " + call).encode())
    assert summary["filter_changes"] == summary["rejected"] == 1
    assert summary["loopback"] == summary["connected_send"] == 0
    assert "0x1234" not in json.dumps(summary)


def test_ac_15_split_filter_install_is_reconstructed_and_rejected():
    trace = (
        "42 seccomp(SECCOMP_SET_MODE_FILTER, 0, <unfinished ...>\n"
        "43 prctl(PR_GET_SECCOMP) = 2\n"
        "42 <... seccomp resumed>{len=4, filter=0x1234}) = 0"
    )
    summary = offline.trace_summary(trace.encode())
    assert summary["filter_changes"] == summary["rejected"] == summary["control_queries"] == 1


def test_ac_15_grandchild_canary_cannot_pass_without_observed_attempt(monkeypatch):
    calls = 0

    def collect(command, **kwargs):
        nonlocal calls
        calls += 1
        return completed(command), {"rejected": int(calls == 1), "unix": int(calls == 2)}

    monkeypatch.setattr(offline, "collect_trace", collect)
    with pytest.raises(VerificationFailure, match="grandchild_not_rejected"):
        offline.prove_trace_guard()
    assert calls == 3


@pytest.mark.parametrize(
    "call",
    [
        "seccomp(0x1, 0, NULL) = 0",
        "seccomp(SECCOMP_UNKNOWN, 0, NULL) = -1 EINVAL",
        "seccomp(SECCOMP_GET_ACTION_AVAIL, 1, [SECCOMP_RET_LOG]) = 0",
        "seccomp(SECCOMP_GET_ACTION_AVAIL, 0, 0x1234) = -1 EFAULT",
        "seccomp(SECCOMP_GET_ACTION_AVAIL_PREFIX, 0, [SECCOMP_RET_LOG]) = 0",
        "seccomp(SECCOMP_GET_NOTIF_SIZES, 0, {seccomp_notif=80}) = 0",
        "prctl(PR_SET_SYSCALL_USER_DISPATCH, PR_SYS_DISPATCH_ON, 0, 0, 0) = 0",
        "prctl(PR_SET_DUMPABLE, SUID_DUMP_DISABLE) = 0",
        "prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY) = 0",
        "prctl(0x16, 2, 0x1234) = 0",
        "prctl(PR_SET_NO_NEW_PRIVS, 0, 0, 0, 0) = -1 EINVAL",
        "prctl(PR_SET_NAME, opaque) = -1 EFAULT",
        "prctl(PR_GET_SECCOMP, 0x1234) = 2",
        "seccomp(SECCOMP_SET_MODE_FILTER, 0, <unfinished ...>",
        "prctl(PR_SET_SECCOMP, <unfinished ...>\n+++ killed by SIGKILL +++",
        "invented_syscall(0) = 0",
    ],
)
def test_ac_15_unknown_control_operations_and_incomplete_install_fail_closed(call):
    with pytest.raises(VerificationFailure):
        offline.trace_summary(call.encode())


def test_ac_15_only_explicit_readonly_queries_and_safe_prctl_forms_pass():
    trace = "\n".join(
        [
            "1 seccomp(SECCOMP_GET_ACTION_AVAIL, 0, [SECCOMP_RET_LOG]) = 0",
            "1 seccomp(SECCOMP_GET_NOTIF_SIZES, 0, "
            "{seccomp_notif=80, seccomp_notif_resp=24, seccomp_data=64}) = 0",
            "1 prctl(PR_GET_SECCOMP) = 2",
            "1 prctl(PR_GET_DUMPABLE) = 1",
            "1 prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) = 1",
            '1 prctl(PR_GET_NAME, ""...) = 0',
            '1 prctl(PR_SET_NAME, "SECCOMP_SET_MODE_FILTER PR_SET_SECCOMP") = 0',
            "1 prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) = 0",
        ]
    )
    summary = offline.trace_summary(trace.encode())
    assert summary["control_queries"] == 6 and summary["safe_prctl"] == 2
    assert summary["filter_changes"] == summary["rejected"] == 0


@pytest.mark.parametrize("capability", ["CAP_MAC_OVERRIDE", "0x30", "0x2c", "0x2a"])
def test_ac_15_observed_chromium_capability_queries_are_readonly(capability):
    argument = capability if capability.startswith("CAP_") else capability + " /* CAP_??? */"
    summary = offline.trace_summary(f"42 prctl(PR_CAPBSET_READ, {argument}) = -1 EINVAL".encode())
    assert summary["control_queries"] == 1 and summary["rejected"] == 0
    assert len(offline.TRACE_CAPABILITIES) == 41


def test_ac_15_observed_chromium_vector_queries_and_vma_labels_include_split_calls():
    trace = "\n".join(
        [
            "41 prctl(PR_SME_GET_VL) = -1 EINVAL",
            "42 prctl(PR_SVE_GET_VL <unfinished ...>",
            "43 prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, 0x10000, 4295950336, <unfinished ...>",
            '44 prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, 0x4000000000, 34359738368, ""...) = 0',
            "42 <... prctl resumed>) = -1 EINVAL",
            '43 <... prctl resumed>""...) = -1 EINVAL',
        ]
    )
    summary = offline.trace_summary(trace.encode())
    assert summary["control_queries"] == summary["safe_prctl"] == 2
    assert summary["rejected"] == summary["filter_changes"] == 0
    assert "0x10000" not in json.dumps(summary)


@pytest.mark.parametrize(
    "call",
    [
        "prctl(PR_CAPBSET_READ, CAP_INVENTED) = -1 EINVAL",
        "prctl(PR_CAPBSET_READ, 48) = -1 EINVAL",
        "prctl(PR_CAPBSET_READ, 0x30) = -1 EINVAL",
        "prctl(PR_CAPBSET_READ, 0x100000000 /* CAP_??? */) = -1 EINVAL",
        "prctl(PR_CAPBSET_READ, 0x30 /* OTHER_??? */) = -1 EINVAL",
        "prctl(PR_CAPBSET_READ, CAP_MAC_OVERRIDE, 0) = 0",
        "prctl(PR_CAPBSET_DROP, CAP_MAC_OVERRIDE) = 0",
        "prctl(PR_SME_GET_VL, 0) = -1 EINVAL",
        "prctl(PR_SVE_SET_VL, 0x10) = 0",
        'prctl(PR_SET_VMA, PR_SET_VMA_OTHER, 0x10000, 4096, ""...) = 0',
        'prctl(PR_SET_VMA, 0, 0x10000, 4096, ""...) = 0',
        'prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, opaque, 4096, ""...) = -1 EFAULT',
        'prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, 0x10000000000000000, 4096, ""...) = -1 EINVAL',
        'prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, 0x10000, 18446744073709551616, ""...) = -1 EINVAL',
        'prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, 0x10000, -1, ""...) = -1 EINVAL',
        "prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, 0x10000, 4096, 0x1234) = -1 EFAULT",
        'prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, 0x10000, 4096, ""..., 0) = 0',
    ],
)
def test_ac_15_chromium_allowances_do_not_allow_unknown_or_mutating_controls(call):
    with pytest.raises(VerificationFailure):
        offline.trace_summary(call.encode())


@pytest.mark.parametrize(
    ("stdout", "code", "rejected", "filter_changes"),
    [
        (b'{"filter_installed": false, "masked_connect_errno": 1}', 0, 1, 1),
        (b'{"filter_installed": true, "masked_connect_errno": 101}', 0, 1, 1),
        (b'{"filter_installed": true, "masked_connect_errno": 1}', 1, 1, 1),
        (b'{"filter_installed": true, "masked_connect_errno": 1}', 0, 0, 0),
        (b'{"filter_installed": true, "masked_connect_errno": 1}', 0, 2, 1),
    ],
)
def test_ac_15_mask_canary_requires_real_success_and_observed_install(
    monkeypatch, stdout, code, rejected, filter_changes
):
    calls = 0

    def collect(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            return completed(command, stdout, code), {
                "rejected": rejected,
                "filter_changes": filter_changes,
            }
        return completed(command), {"rejected": int(calls != 2), "unix": int(calls == 2)}

    monkeypatch.setattr(offline, "collect_trace", collect)
    with pytest.raises(VerificationFailure, match="masking_filter_not_rejected"):
        offline.prove_trace_guard()


def test_ac_15_tracing_setup_diagnostic_never_silently_falls_back(monkeypatch):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, b"", b"strace: seccomp-bpf unavailable; private diagnostic\n"
        )

    monkeypatch.setattr(offline.subprocess, "run", run)
    with pytest.raises(VerificationFailure, match="setup_or_runtime_diagnostic") as error:
        offline.collect_trace(("python", "child.py"), timeout=1)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "trace,code",
    [
        (b"opaque record", "network_trace_unclassified_record"),
        (b"\xff", "UnicodeDecodeError"),
        (
            b'1 connect(3, {sa_family=AF_INET, sin_addr=inet_addr("192.0.2.1")}, 16) = -1',
            "external_network_syscall_observed",
        ),
    ],
)
def test_ac_15_failed_audit_retains_child_result_without_changing_rejection(
    monkeypatch, trace, code
):
    child = completed(["child"], b"private captured output", code=3)

    def run(command, **kwargs):
        Path(command[command.index("-o") + 1]).write_bytes(trace)
        return child

    monkeypatch.setattr(offline.subprocess, "run", run)
    with pytest.raises(offline.TracedCommandFailure) as error:
        offline.traced_command(("child",), timeout=1)
    assert str(error.value) == code
    assert error.value.result is child
    evidence = offline.failed_command_evidence(
        ("child",), elapsed=1.2345, error=error.value, suite="browser"
    )
    assert evidence["exit_code"] == 3 and evidence["output_complete"] is True
    assert evidence["stdout_sha256"] == hashlib.sha256(child.stdout).hexdigest()
    assert "private" not in json.dumps(evidence)


@pytest.mark.parametrize("failure", ["timeout", "trace"])
def test_ac_15_backend_failure_preserves_only_bounded_source_allowlisted_progress(
    tmp_path, monkeypatch, failure
):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_known.py").write_text("def test_known(): pass\n")
    monkeypatch.setattr(offline, "ROOT", tmp_path)
    events = [
        {"event": "stage_started", "stage": "pytest"},
        {
            "event": "test_started",
            "stage": "pytest",
            "node_id": "tests/test_known.py::test_known[private-parameter]",
        },
        {"event": "test_started", "stage": "pytest", "node_id": "private-untrusted-node"},
    ]
    stdout = b"private raw output\n" + b"\n".join(
        ("DEL05_OFFLINE_DIAGNOSTIC " + json.dumps(event)).encode() for event in events
    )
    stderr = b"private error output"
    error = (
        subprocess.TimeoutExpired(["private command"], 3, output=stdout, stderr=stderr)
        if failure == "timeout"
        else offline.TracedCommandFailure(
            "network_trace_unclassified_record",
            subprocess.CompletedProcess(["private command"], 1, stdout, stderr),
        )
    )
    evidence = offline.failed_command_evidence(
        offline.SUITES["backend"][0], elapsed=3.001, error=error, suite="backend"
    )
    assert evidence["argv"] == list(offline.SUITES["backend"][0])
    assert evidence["duration_seconds"] == 3.001
    assert evidence["output_complete"] is (failure == "trace")
    assert ("exit_code" in evidence) is (failure == "trace")
    assert evidence["stdout_sha256"] == hashlib.sha256(stdout).hexdigest()
    assert evidence["stderr_sha256"] == hashlib.sha256(stderr).hexdigest()
    assert evidence["diagnostics"]["last_stage"] == "pytest"
    assert evidence["diagnostics"]["active_test"] == "tests/test_known.py::test_known"
    assert "private" not in json.dumps(evidence)


@pytest.mark.parametrize(
    "error",
    [
        VerificationFailure("source_content_changed"),
        FileNotFoundError("private trace path and credential"),
        subprocess.TimeoutExpired(["private command"], 10),
    ],
)
def test_ac_15_inside_failure_emits_one_safe_actionable_result(monkeypatch, capsys, error):
    for key, value in MANIFEST.items():
        monkeypatch.setenv("AC15_" + key.upper(), value)

    def fail(suite):
        raise error

    monkeypatch.setattr(offline, "inside", fail)
    assert offline.main(["inside", "--suite", "backend"]) == 1
    output = capsys.readouterr().out
    assert output.count(offline.PREFIX) == 1 and "private" not in output
    result = json.loads(output.removeprefix(offline.PREFIX))
    assert result["ok"] is False and result["source_commit"] == COMMIT
    assert result["failure"] in {"source_content_changed", "FileNotFoundError", "TimeoutExpired"}


@pytest.mark.parametrize(
    "error",
    [
        VerificationFailure("network_trace_unclassified_record"),
        FileNotFoundError("private trace path and credential"),
        subprocess.TimeoutExpired(["private command"], 10, output=b"private output"),
        ValueError("private malformed IP address"),
        UnicodeDecodeError("utf-8", b"private\xff", 7, 8, "private malformed trace"),
    ],
)
def test_ac_15_trace_failure_keeps_passed_commands_and_fixed_failure_identity(
    tmp_path, monkeypatch, capsys, error
):
    for key, value in offline.SAFE_ENV.items():
        monkeypatch.setenv(key, value)
    for key, value in MANIFEST.items():
        monkeypatch.setenv("AC15_" + key.upper(), value)
    read_text = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            json.dumps(MANIFEST)
            if path == Path("/opt/ac15/source-manifest.json")
            else read_text(path, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(offline, "ROOT", tmp_path)
    monkeypatch.setattr(offline.os, "getuid", lambda: 10001)
    monkeypatch.setattr(offline, "verify_source", lambda *args: None)
    monkeypatch.setattr(offline, "kernel_canaries", lambda: {"kernel_proof": True})
    monkeypatch.setattr(offline, "install_browser_transport", lambda: {"profile_installed": True})
    monkeypatch.setattr(
        offline, "prove_browser_transport", lambda collect: {"profile_proved": True}
    )
    monkeypatch.setattr(offline, "prove_trace_guard", lambda: {"trace_proof": True})
    calls = []

    def traced(command, **kwargs):
        calls.append(command)
        if len(calls) == 2:
            raise error
        return completed(command, b"private success output"), {"rejected": 0}

    monkeypatch.setattr(offline, "traced_command", traced)
    assert offline.main(["inside", "--suite", "browser"]) == 1
    output = capsys.readouterr().out
    assert output.count(offline.PREFIX) == 1 and "private" not in output
    result = json.loads(output.removeprefix(offline.PREFIX))
    assert calls == list(offline.SUITES["browser"])
    assert result["ok"] is False and result["source_commit"] == COMMIT
    assert result["kernel"] == {
        "kernel_proof": True,
        "trace_proof": True,
        "browser_transport": {"profile_installed": True, "profile_proved": True},
    }
    assert len(result["commands"]) == 1
    assert result["commands"][0]["argv"] == list(offline.SUITES["browser"][0])
    assert result["commands"][0]["exit_code"] == 0
    assert (
        result["commands"][0]["stdout_sha256"]
        == hashlib.sha256(b"private success output").hexdigest()
    )
    assert result["failed_command"]["argv"] == list(offline.SUITES["browser"][1])
    assert result["failed_command"]["duration_seconds"] >= 0
    assert result["failure"] in {
        "network_trace_unclassified_record",
        "FileNotFoundError",
        "TimeoutExpired",
        "ValueError",
        "UnicodeDecodeError",
    }


@pytest.mark.parametrize("suite", ["backend", "frontend", "browser", "native"])
def test_ac_15_transport_profile_is_browser_only_before_all_trace_proofs(monkeypatch, suite):
    for key, value in offline.SAFE_ENV.items():
        monkeypatch.setenv(key, value)
    read_text = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, *args, **kwargs: (
            json.dumps(MANIFEST)
            if path == Path("/opt/ac15/source-manifest.json")
            else read_text(path, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(offline.os, "getuid", lambda: 10001)
    observed = []
    monkeypatch.setattr(offline, "verify_source", lambda *args: observed.append("source"))

    def kernel():
        observed.append("kernel")
        return {}

    def install():
        assert suite == "browser", "another suite must not install the browser policy"
        observed.append("install")
        return {}

    def prove(collect):
        assert suite == "browser" and collect is offline.collect_trace
        observed.append("profile-proof")
        return {}

    def trace():
        observed.append("trace-proof")
        # Stop at this boundary without installing a host filter or running tests.
        raise RuntimeError("test proof boundary")

    monkeypatch.setattr(offline, "kernel_canaries", kernel)
    monkeypatch.setattr(offline, "install_browser_transport", install)
    monkeypatch.setattr(offline, "prove_browser_transport", prove)
    monkeypatch.setattr(offline, "prove_trace_guard", trace)
    with pytest.raises(RuntimeError, match="test proof boundary"):
        offline.inside(suite)
    assert observed == [
        "source",
        "kernel",
        *(["install", "profile-proof"] if suite == "browser" else []),
        "trace-proof",
    ]


def interface(sysfs, name, flags, state):
    path = sysfs / name
    path.mkdir(exist_ok=True)
    (path / "flags").write_text(flags + "\n")
    (path / "operstate").write_text(state + "\n")


def namespace_fixture(tmp_path):
    sysfs = tmp_path / "net"
    sysfs.mkdir()
    interface(sysfs, "lo", "0x9", "unknown")
    (sysfs / "bonding_masters").write_text("")
    ipv4, ipv6 = tmp_path / "route", tmp_path / "ipv6_route"
    ipv4.write_text("Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n")
    ipv6.write_text(
        "0" * 32
        + " 00 "
        + "0" * 32
        + " 00 "
        + "0" * 32
        + " ffffffff 00000001 00000000 00200200 lo\n"
    )
    return sysfs, ipv4, ipv6


def test_ac_15_dormant_kernel_tunnels_do_not_create_egress(tmp_path):
    sysfs, ipv4, ipv6 = namespace_fixture(tmp_path)
    for name in ("tunl0", "gre0", "ip_vti0", "ip6_vti0", "sit0", "ip6tnl0", "ip6gre0"):
        interface(sysfs, name, "0x80", "down")
    for name in ("gretap0", "erspan0"):
        interface(sysfs, name, "0x1002", "down")
    proof = offline.kernel_namespace(sysfs, ipv4, ipv6)
    assert proof["active_interfaces"] == ["lo"]
    assert "bonding_masters" not in proof["interfaces"]
    assert len(proof["interfaces"]) == 10
    assert proof["ipv4_route_count"] == 0 and proof["ipv6_route_interfaces"] == ["lo"]


@pytest.mark.parametrize(
    ("name", "flags", "state"),
    [
        ("eth0", "0x1", "down"),
        ("eth0", "0x0", "unknown"),
        ("eth0", "not-flags", "down"),
        ("eth0", "0x", "down"),
        ("lo", "0x8", "unknown"),
        ("lo", "0x1", "unknown"),
        ("lo", "0x9", "down"),
    ],
)
def test_ac_15_active_or_unparsed_interface_cannot_pass(tmp_path, name, flags, state):
    sysfs, ipv4, ipv6 = namespace_fixture(tmp_path)
    interface(sysfs, name, flags, state)
    with pytest.raises(VerificationFailure):
        offline.kernel_namespace(sysfs, ipv4, ipv6)


@pytest.mark.parametrize(
    ("version", "content"),
    [
        (4, "Iface invalid header\n"),
        (
            4,
            "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
            "eth0 external\n",
        ),
        (6, "unparsed route\n"),
        (
            6,
            "0" * 32
            + " 00 "
            + "0" * 32
            + " 00 "
            + "0" * 32
            + " ffffffff 00000001 00000000 00200200 eth0\n",
        ),
        (
            6,
            "0" * 32
            + " ff "
            + "0" * 32
            + " 00 "
            + "0" * 32
            + " ffffffff 00000001 00000000 00200200 lo\n",
        ),
    ],
)
def test_ac_15_external_or_unparsed_routes_cannot_pass(tmp_path, version, content):
    sysfs, ipv4, ipv6 = namespace_fixture(tmp_path)
    (ipv4 if version == 4 else ipv6).write_text(content)
    with pytest.raises(VerificationFailure):
        offline.kernel_namespace(sysfs, ipv4, ipv6)
