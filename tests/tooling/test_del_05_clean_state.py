"""The clean verifier cannot consume worktree extras or clean unrelated storage."""

from __future__ import annotations

import io
import json
import signal
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import del_05_clean_state as clean_state
from scripts.del_05_clean_state import (
    Verification,
    VerificationFailure,
    clean_environment,
    export_archive,
    tree_fingerprint,
    validate_compose_boundary,
    validate_container_network,
    validate_docker_endpoint,
    validate_docker_engine_version,
    validate_owned_resource,
    validate_ref,
)
from scripts.del_05_runtime_smoke import LocalClient, SmokeFailure


def archive_member(path: Path, name: str, *, kind: bytes = tarfile.REGTYPE) -> None:
    with tarfile.open(path, "w") as archive:
        item = tarfile.TarInfo(name)
        item.type = kind
        item.linkname = "/outside" if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE} else ""
        item.size = 5 if kind == tarfile.REGTYPE else 0
        archive.addfile(item, io.BytesIO(b"hello") if item.size else None)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../outside", tarfile.REGTYPE),
        ("/outside", tarfile.REGTYPE),
        ("link", tarfile.SYMTYPE),
        ("hardlink", tarfile.LNKTYPE),
        ("../directory", tarfile.DIRTYPE),
        ("pipe", tarfile.FIFOTYPE),
        (".env", tarfile.REGTYPE),
        ("nested/.env.local", tarfile.REGTYPE),
    ],
)
def test_del_05_archive_rejects_escape_links_and_environment_files(tmp_path, name, kind):
    archive = tmp_path / "export.tar"
    destination = tmp_path / "source"
    destination.mkdir()
    archive_member(archive, name, kind=kind)
    with pytest.raises(VerificationFailure):
        export_archive(archive, destination)
    assert list(destination.iterdir()) == []
    assert not (tmp_path / "outside").exists()


def test_del_05_archive_prevalidates_every_member_before_writes(tmp_path):
    archive = tmp_path / "export.tar"
    with tarfile.open(archive, "w") as source:
        first = tarfile.TarInfo("innocent")
        first.size = 2
        source.addfile(first, io.BytesIO(b"ok"))
        source.addfile(tarfile.TarInfo("../outside"))
    destination = tmp_path / "source"
    destination.mkdir()
    with pytest.raises(VerificationFailure):
        export_archive(archive, destination)
    assert list(destination.iterdir()) == []


def test_del_05_exact_commit_export_excludes_worktree_ignored_and_untracked_files(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()

    def git(*arguments):
        return subprocess.run(
            ["git", "-C", str(repository), *arguments], check=True, capture_output=True
        )

    git("init", "--quiet")
    (repository / "tracked").write_text("committed")
    (repository / ".gitignore").write_text("ignored\n")
    git("add", "tracked", ".gitignore")
    git(
        "-c",
        "user.name=DEL-05 test",
        "-c",
        "user.email=del05@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "isolated test fixture",
    )
    (repository / "tracked").write_text("dirty caller work")
    (repository / "untracked").write_text("do not export")
    (repository / "ignored").write_text("do not export")
    before = git("status", "--porcelain=v1", "-z").stdout
    archive = tmp_path / "selected.tar"
    git("archive", "--format=tar", f"--output={archive}", "HEAD")
    destination = tmp_path / "source"
    destination.mkdir()
    export_archive(archive, destination)
    assert (destination / "tracked").read_text() == "committed"
    assert not (destination / "ignored").exists()
    assert not (destination / "untracked").exists()
    assert git("status", "--porcelain=v1", "-z").stdout == before


def test_del_05_environment_allowlist_removes_credentials_and_compose_overrides():
    source = {
        "PATH": "/safe/bin",
        "HOME": "/user",
        "AWS_ACCESS_KEY_ID": "private",
        "OPENAI_API_KEY": "private",
        "GOOGLE_APPLICATION_CREDENTIALS": "/private/key",
        "DATABASE_URL": "sqlite:///developer.db",
        "COMPOSE_FILE": "/outside/compose.yaml",
        "HTTP_PROXY": "http://proxy.invalid",
        "PYTHONPATH": "/attacker",
        "BUILDX_BUILDER": "remote-builder",
    }
    clean = clean_environment(source)
    assert set(clean) == {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONDONTWRITEBYTECODE",
        "COMPOSE_DISABLE_ENV_FILE",
        "BUILDX_BUILDER",
    }
    assert "private" not in json.dumps(clean)
    assert clean["BUILDX_BUILDER"] == "default"


def test_del_05_every_acquisition_command_forces_default_local_builder(tmp_path, monkeypatch):
    verifier = Verification(tmp_path, "HEAD")
    verifier.source = tmp_path
    verifier.compose = ["docker", "compose"]
    monkeypatch.setattr(verifier, "prepare", lambda: None)
    calls = []

    def command(label, arguments, **kwargs):
        if label == "start-fresh-runtime":
            raise VerificationFailure("end-of-acquisition-test")
        calls.append((label, arguments))
        return subprocess.CompletedProcess(arguments, 0, b"", b"")

    monkeypatch.setattr(verifier, "command", command)
    with pytest.raises(VerificationFailure, match="end-of-acquisition-test"):
        verifier.execute()
    assert len(calls) == 3
    for _, arguments in calls:
        assert arguments[arguments.index("--builder") + 1] == "default"


def test_del_05_execution_expiry_still_cleans_and_restores_signal_state(
    tmp_path, monkeypatch, capsys
):
    verifier = Verification(tmp_path, "HEAD")
    cleaned = []
    original_handlers = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
    }
    original_timer = signal.getitimer(signal.ITIMER_REAL)
    monkeypatch.setattr(clean_state, "Verification", lambda *args: verifier)
    monkeypatch.setattr(clean_state, "EXECUTION_SECONDS", 0.02)
    monkeypatch.setattr(verifier, "execute", lambda: time.sleep(1))

    def cleanup():
        cleaned.append(True)
        assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        verifier.report["cleanup"] = {"ok": True}

    monkeypatch.setattr(verifier, "cleanup", cleanup)
    assert clean_state.main([]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failure"] == "verification_execution_deadline"
    assert report["cleanup"] == {"ok": True}
    assert cleaned == [True]
    assert {signum: signal.getsignal(signum) for signum in original_handlers} == original_handlers
    assert signal.getitimer(signal.ITIMER_REAL) == original_timer


def test_del_05_preexisting_signal_handlers_and_timer_are_restored():
    previous = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def caller_handler(_signum, _frame):
        pass

    try:
        signal.signal(signal.SIGALRM, caller_handler)
        signal.setitimer(signal.ITIMER_REAL, 60, 60)
        with (
            pytest.raises(VerificationFailure, match="execution_deadline"),
            clean_state.verification_signals(10),
        ):
            signal.raise_signal(signal.SIGALRM)
        assert signal.getsignal(signal.SIGALRM) is caller_handler
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 0 < remaining <= 60
        assert interval == 60
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def test_del_05_cleanup_commands_share_one_budget_and_never_skip_export_removal(
    tmp_path, monkeypatch
):
    verifier = Verification(tmp_path, "HEAD")
    verifier.created = True
    verifier.compose = ["docker", "compose", "--project-name", verifier.project]
    verifier.temporary = Path(tempfile.mkdtemp(prefix="marketing-agents-del05-")).resolve()
    export = verifier.temporary
    previous_deadline = verifier.cleanup_deadline
    monkeypatch.setattr(clean_state, "CLEANUP_SECONDS", 0)
    with patch("scripts.del_05_clean_state.subprocess.run") as execute:
        verifier.cleanup()
    execute.assert_not_called()
    assert not export.exists()
    assert verifier.cleanup_deadline is previous_deadline
    assert verifier.report["cleanup"]["failures"] == ["compose_cleanup_failed"]


def test_del_05_cleanup_caps_each_subprocess_to_remaining_aggregate_budget(tmp_path):
    verifier = Verification(tmp_path, "HEAD")
    verifier.cleanup_deadline = time.monotonic() + 2
    with patch("scripts.del_05_clean_state.subprocess.run") as execute:
        execute.return_value = subprocess.CompletedProcess(["docker"], 0, b"", b"")
        verifier.command("cleanup-test", ["docker"], timeout=300)
    assert 0 < execute.call_args.kwargs["timeout"] <= 2


def test_del_05_internal_deadlines_leave_outer_make_gate_cleanup_margin():
    assert clean_state.EXECUTION_SECONDS == 1620
    assert clean_state.CLEANUP_SECONDS == 120
    assert clean_state.EXECUTION_SECONDS + clean_state.CLEANUP_SECONDS < 1800


@pytest.mark.parametrize("ref", ["", "--all", "HEAD;true", "$(touch /tmp/bad)", "HEAD\nmain"])
def test_del_05_unsafe_source_refs_fail_before_subprocess(ref):
    with pytest.raises(VerificationFailure):
        validate_ref(ref)


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp://127.0.0.1:2375",
        "ssh://remote.example",
        "tcp://remote.example:2376",
        "unix://remote/path",
        "unix:///",
        "unix:///tmp/docker.sock?override=1",
    ],
)
def test_del_05_refuses_remote_or_ambiguous_docker_engine(endpoint):
    with pytest.raises(VerificationFailure, match="local_unix_docker_engine"):
        validate_docker_endpoint(endpoint)


def test_del_05_accepts_explicit_local_docker_socket_only():
    assert validate_docker_endpoint("unix:///var/run/docker.sock") == "unix:///var/run/docker.sock"


@pytest.mark.parametrize("version", [None, "", "unknown", "27.9.9", "28junk", "-28"])
def test_del_05_requires_supported_docker_engine_version(version):
    with pytest.raises(VerificationFailure, match="docker_engine_28"):
        validate_docker_engine_version(version)


@pytest.mark.parametrize("version", ["28.0.0", "29.1.1", "28.0.0-beta.1"])
def test_del_05_accepts_supported_docker_engine_version(version):
    validate_docker_engine_version(version)


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.com",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000@evil.test",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:8000?next=evil",
    ],
)
def test_del_05_smoke_refuses_any_non_loopback_authority(origin):
    with pytest.raises((SmokeFailure, ValueError)):
        LocalClient(origin)


@pytest.mark.parametrize(
    "socket_path", ["/tmp/other.sock", "/var/run/docker.sock", "relative.sock"]
)
def test_del_05_smoke_refuses_arbitrary_unix_socket_targets(socket_path):
    with pytest.raises(SmokeFailure, match="fixed_api_socket"):
        LocalClient("http://127.0.0.1:8000", unix_socket=socket_path)


def test_del_05_smoke_uses_real_unix_http_transport_without_tcp_or_proxy(monkeypatch):
    # Keep the socket path below sockaddr_un's platform-specific byte limit.
    with tempfile.TemporaryDirectory(prefix="del05-uds-", dir="/tmp") as directory:
        path = str(Path(directory) / "api.sock")
        monkeypatch.setattr("scripts.del_05_runtime_smoke.API_SOCKET", path)
        monkeypatch.setenv("HTTP_PROXY", "http://unreachable.invalid:9999")
        received = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            server.settimeout(5)

            def respond():
                connection, _ = server.accept()
                with connection:
                    received.append(connection.recv(4096))
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b'Content-Length: 18\r\nConnection: close\r\n\r\n{"status":"ready"}'
                    )

            worker = threading.Thread(target=respond, daemon=True)
            worker.start()
            client = LocalClient("http://127.0.0.1:8000", unix_socket=path)
            try:
                assert client.request("/health/ready") == {"status": "ready"}
            finally:
                worker.join(timeout=6)
            assert not worker.is_alive()
            assert b"GET /health/ready HTTP/1.1" in received[0]
            assert b"Host: 127.0.0.1:8000" in received[0]


def test_del_05_cleanup_refuses_broad_names_and_foreign_labels():
    project = "marketing-agents-del05-0123456789abcdef"
    labels = {"Labels": {"com.docker.compose.project": project}}
    validate_owned_resource("volume", project + "_data", project, labels)
    for name in ("/", "data", "marketing-agents_data", project + "_unrelated"):
        with pytest.raises(VerificationFailure, match="unsafe_cleanup_target"):
            validate_owned_resource("volume", name, project, labels)
    with pytest.raises(VerificationFailure, match="cleanup_ownership_mismatch"):
        validate_owned_resource("volume", project + "_data", project, {"Labels": {}})


def test_del_05_cleanup_failure_is_failure_and_never_widens_scope(tmp_path):
    verifier = Verification(tmp_path, "HEAD")
    verifier.created = True
    verifier.compose = ["docker", "compose", "--project-name", verifier.project]
    verifier.report["ok"] = True
    calls = []

    def command(label, args, **kwargs):
        calls.append(args)
        if "inspect" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [{"Labels": {"com.docker.compose.project": "unrelated-project"}}]
                ).encode(),
                b"",
            )
        raise AssertionError("Must not execute cleanup after ownership rejection")

    with patch.object(verifier, "command", side_effect=command):
        verifier.cleanup()
    assert not verifier.report["ok"]
    assert verifier.report["cleanup"]["failures"] == ["compose_cleanup_failed"]
    assert all("down" not in command for command in calls)


def test_del_05_cleanup_runs_after_timeout_and_reports_no_raw_output(tmp_path):
    verifier = Verification(tmp_path, "HEAD")
    with (
        patch(
            "scripts.del_05_clean_state.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                ["docker"], 1, output=b"private-canary", stderr=b"private-canary"
            ),
        ),
        pytest.raises(VerificationFailure, match="command_timeout"),
    ):
        verifier.command("bounded-test", ["docker"], timeout=1)
    verifier.cleanup()
    assert verifier.report["cleanup"]["ok"]
    assert "private-canary" not in json.dumps(verifier.report)
    assert verifier.report["commands"][-1]["timeout"] == 1


def test_del_05_export_reproducibility_detects_modified_and_new_files(tmp_path):
    (tmp_path / "source").write_text("original")
    before = tree_fingerprint(tmp_path)
    (tmp_path / "source").write_text("changed")
    assert tree_fingerprint(tmp_path) != before
    (tmp_path / "source").write_text("original")
    (tmp_path / "generated").write_text("unexpected generated output")
    assert tree_fingerprint(tmp_path) != before


def compose_config(project):
    config = {
        "volumes": {
            name: {"name": f"{project}_{name}"} for name in ("data", "local-secrets", "api-socket")
        },
        "networks": {
            "default": {
                "name": f"{project}_default",
                "internal": False,
                "driver_opts": {"com.docker.network.bridge.gateway_mode_ipv4": "nat"},
            }
        },
        "services": {
            name: {"image": f"{project}-{'web' if name == 'web' else 'backend'}:local"}
            for name in (
                "web",
                "api",
                "local-secret-init",
                "migrate-seed",
                "run-worker",
                "scheduler-worker",
            )
        },
    }
    for name, service in config["services"].items():
        if name == "web":
            service["networks"] = {"default": None}
            service["ports"] = [{"host_ip": "127.0.0.1", "target": 8080, "published": "18080"}]
            service["volumes"] = [
                {
                    "type": "volume",
                    "source": "api-socket",
                    "target": "/var/run/marketing-agents",
                    "read_only": True,
                }
            ]
        else:
            service["network_mode"] = "none"
            service["volumes"] = [
                {
                    "type": "volume",
                    "source": "data",
                    "target": "/var/lib/marketing-agents/data",
                    "read_only": name == "local-secret-init",
                },
                {
                    "type": "volume",
                    "source": "local-secrets",
                    "target": "/var/lib/marketing-agents/secrets",
                    "read_only": name != "local-secret-init",
                },
            ]
            if name == "api":
                service["environment"] = {
                    "MARKETING_AGENTS_API_SOCKET": "/var/run/marketing-agents/api.sock"
                }
                service["volumes"].append(
                    {
                        "type": "volume",
                        "source": "api-socket",
                        "target": "/var/run/marketing-agents",
                    }
                )
    return config


@pytest.mark.parametrize(
    "field,value",
    [
        ("volumes", [{"type": "bind", "source": "/Users", "target": "/app"}]),
        ("network_mode", "host"),
        ("privileged", True),
        ("env_file", ["/private/.env"]),
        ("ports", [{"host_ip": "0.0.0.0", "target": 8000}]),
        ("build", {"context": "/outside"}),
        ("image", "marketing-agents-local-backend:local"),
    ],
)
def test_del_05_compose_refuses_external_mounts_networks_and_contexts(tmp_path, field, value):
    project = "marketing-agents-del05-0123456789abcdef"
    config = compose_config(project)
    validate_compose_boundary(config, project, tmp_path)
    config["services"]["api"][field] = value
    with pytest.raises(VerificationFailure):
        validate_compose_boundary(config, project, tmp_path)


def test_del_05_compose_requires_documented_web_ingress_nat_profile():
    project = "marketing-agents-del05-0123456789abcdef"
    config = compose_config(project)
    config["networks"]["default"].pop("driver_opts")
    with pytest.raises(VerificationFailure, match="nat_gateway"):
        validate_compose_boundary(config, project)


def test_del_05_compose_accepts_canonical_omitted_false_network_setting():
    project = "marketing-agents-del05-0123456789abcdef"
    config = compose_config(project)
    config["networks"]["default"].pop("internal")
    config["networks"]["default"]["ipam"] = {}
    validate_compose_boundary(config, project)
    config["networks"]["default"]["internal"] = True
    with pytest.raises(VerificationFailure, match="web_ingress_network_must_be_scoped"):
        validate_compose_boundary(config, project)


@pytest.mark.parametrize("volume", ["data", "local-secrets", "api-socket"])
def test_del_05_compose_refuses_named_volume_host_bind_disguises(volume):
    project = "marketing-agents-del05-0123456789abcdef"
    config = compose_config(project)
    config["volumes"][volume]["driver_opts"] = {"type": "none", "o": "bind", "device": "/Users"}
    with pytest.raises(VerificationFailure, match="unsafe_compose_volume"):
        validate_compose_boundary(config, project)


@pytest.mark.parametrize(
    "service", ["api", "run-worker", "scheduler-worker", "local-secret-init", "migrate-seed"]
)
def test_del_05_compose_refuses_any_backend_network_attachment(service):
    project = "marketing-agents-del05-0123456789abcdef"
    config = compose_config(project)
    config["services"][service]["network_mode"] = "service:web"
    with pytest.raises(VerificationFailure, match="backend_requires_network_none"):
        validate_compose_boundary(config, project)


def test_del_05_compose_refuses_web_sensitive_mount_and_writable_socket():
    project = "marketing-agents-del05-0123456789abcdef"
    config = compose_config(project)
    config["services"]["web"]["volumes"][0]["read_only"] = False
    with pytest.raises(VerificationFailure, match="unexpected_service_mounts"):
        validate_compose_boundary(config, project)
    config = compose_config(project)
    config["services"]["web"]["volumes"].append(
        {"type": "volume", "source": "data", "target": "/var/lib/marketing-agents/data"}
    )
    with pytest.raises(VerificationFailure, match="unexpected_service_mounts"):
        validate_compose_boundary(config, project)


@pytest.mark.parametrize(
    "ports", [{}, {"8080/tcp": []}, {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18080"}]}]
)
def test_del_05_runtime_refuses_healthy_web_without_exact_loopback_publication(ports):
    project = "marketing-agents-del05-0123456789abcdef"
    inspected = {
        "Config": {
            "Labels": {"com.docker.compose.project": project, "com.docker.compose.service": "web"}
        },
        "HostConfig": {"NetworkMode": f"{project}_default"},
        "NetworkSettings": {"Networks": {f"{project}_default": {}}, "Ports": ports},
    }
    with pytest.raises(VerificationFailure, match="runtime_loopback_port_not_published"):
        validate_container_network(inspected, "web", project, 18080)
    inspected["NetworkSettings"]["Ports"] = {
        "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]
    }
    validate_container_network(inspected, "web", project, 18080)


def test_del_05_runtime_refuses_configured_backend_attached_to_web_namespace():
    project = "marketing-agents-del05-0123456789abcdef"
    inspected = {
        "Config": {
            "Labels": {"com.docker.compose.project": project, "com.docker.compose.service": "api"}
        },
        "HostConfig": {"NetworkMode": "container:abc123"},
        "NetworkSettings": {"Networks": {}, "Ports": {}},
    }
    with pytest.raises(VerificationFailure, match="runtime_backend_network_not_none"):
        validate_container_network(inspected, "api", project, 18080)
    inspected["HostConfig"]["NetworkMode"] = "none"
    validate_container_network(inspected, "api", project, 18080)


def test_del_05_cleanup_down_failure_does_not_skip_temporary_export(tmp_path):
    verifier = Verification(tmp_path, "HEAD")
    verifier.created = True
    verifier.compose = ["docker", "compose", "--project-name", verifier.project]
    verifier.report["ok"] = True
    calls = []

    def command(label, args, **kwargs):
        calls.append(args)
        if "inspect" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps([{"Labels": {"com.docker.compose.project": verifier.project}}]).encode(),
                b"",
            )
        raise VerificationFailure("command_failed:remove-owned-compose-resources")

    with patch.object(verifier, "command", side_effect=command):
        verifier.cleanup()
    assert not verifier.report["cleanup"]["ok"]
    assert not verifier.report["ok"]
    assert calls[-1] == [*verifier.compose, "down", "--volumes", "--timeout", "30"]
    assert not any("prune" in command for command in calls)


def test_del_05_cleanup_rejects_broad_temporary_directory(tmp_path):
    verifier = Verification(tmp_path, "HEAD")
    verifier.temporary = tmp_path
    (tmp_path / "keep").write_text("caller data")
    verifier.cleanup()
    assert verifier.report["cleanup"]["failures"] == ["temporary_export_cleanup_failed"]
    assert (tmp_path / "keep").read_text() == "caller data"
