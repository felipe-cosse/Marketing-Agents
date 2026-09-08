"""Source acquisition, ownership, and deadline boundaries for the opt-in Docker gate."""

from __future__ import annotations

import json
import signal
import subprocess
import time
from types import SimpleNamespace

import pytest

from scripts import local_backup as transport
from scripts import verify_del_05_backup as gate

IMAGE = "sha256:" + "a" * 64
OTHER_IMAGE = "sha256:" + "b" * 64


@pytest.fixture
def acquisition(monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted-host:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "selected-local-context")
    monkeypatch.setenv("BUILDX_BUILDER", "untrusted-builder")
    monkeypatch.setenv("DOCKER_BUILDKIT", "0")
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted-proxy")
    verifier = gate.BackupVerification(build=True)
    state = SimpleNamespace(
        endpoint="unix:///private/tmp/test-local-docker.sock",
        images={"existing-backend:local": (IMAGE, {})},
        calls=[],
        builds=[],
        returncode=0,
        build_error=None,
    )

    class FakeTransport(transport.DockerTransport):
        def command(self, arguments, **kwargs):
            state.calls.append((list(arguments), dict(self.env)))
            if arguments[:2] == ["context", "inspect"]:
                return state.endpoint.encode()
            if arguments[:2] == ["image", "ls"]:
                name = arguments[arguments.index("--filter") + 1].removeprefix("reference=")
                return IMAGE.encode() if name in state.images else b""
            if arguments[:2] == ["image", "inspect"]:
                identity, labels = state.images[arguments[-1]]
                if len(arguments) == 3:
                    return json.dumps([{"Id": identity, "Config": {"Labels": labels}}]).encode()
                return identity.encode()
            if arguments[:2] == ["image", "rm"]:
                del state.images[arguments[-1]]
                return b""
            if arguments[:2] == ["image", "tag"]:
                state.images[arguments[-1]] = (arguments[-2], {})
                return b""
            if arguments[0] in {"ps", "volume", "network"}:
                return b""
            raise AssertionError(arguments)

    def build(arguments, **kwargs):
        state.builds.append((list(arguments), kwargs))
        state.images[verifier.build_tag] = (IMAGE, {transport.RUN_LABEL: verifier.operation})
        if state.build_error:
            raise state.build_error
        return SimpleNamespace(returncode=state.returncode, stdout=b"", stderr=b"")

    monkeypatch.setattr(transport, "DockerTransport", FakeTransport)
    monkeypatch.setattr(gate.subprocess, "run", build)
    docker = FakeTransport(verifier.source_project)
    verifier.transports = [docker]
    return verifier, docker, state


@pytest.mark.parametrize("arguments", ([], ["--build", "--image", "existing-backend:local"]))
def test_del_05_backup_gate_requires_exactly_one_acquisition_option(arguments, monkeypatch):
    monkeypatch.setattr(gate.BackupVerification, "run", lambda self: pytest.fail("No acquisition"))
    with pytest.raises(SystemExit) as exc:
        gate.main(arguments)
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "arguments, expected",
    (
        (["--build"], (None, True)),
        (["--image", "existing-backend:local"], ("existing-backend:local", False)),
    ),
)
def test_del_05_backup_gate_cli_selects_acquisition(arguments, expected, monkeypatch, capsys):
    selected = []

    def run(self):
        selected.append((self.image, self.build))
        return {"ok": True}

    monkeypatch.setattr(gate.BackupVerification, "run", run)
    assert gate.main(arguments) == 0
    assert selected == [expected]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_del_05_backup_build_pins_local_daemon_and_current_source(acquisition):
    verifier, docker, state = acquisition
    assert verifier.acquire_image(docker) == IMAGE
    assert state.calls[0][0][:2] == ["context", "inspect"]
    assert state.calls[1][0] == [
        "image",
        "ls",
        "--filter",
        f"reference={verifier.build_tag}",
        "--format",
        "{{.ID}}",
    ]
    assert len(state.builds) == 1
    arguments, options = state.builds[0]
    assert arguments == [
        "docker",
        "build",
        "--builder",
        "default",
        "--pull=false",
        "--target",
        "runtime",
        "--file",
        str(transport.ROOT / "docker/api.Dockerfile"),
        "--label",
        f"{transport.RUN_LABEL}={verifier.operation}",
        "--tag",
        verifier.build_tag,
        str(transport.ROOT),
    ]
    assert options["cwd"] == transport.ROOT
    assert options["timeout"] == gate.EXECUTION_TIMEOUT
    assert options["env"]["DOCKER_HOST"] == state.endpoint
    assert (
        not {"DOCKER_CONTEXT", "BUILDX_BUILDER", "DOCKER_BUILDKIT", "HTTP_PROXY"}
        & options["env"].keys()
    )
    assert verifier.report["image_acquisition"] == "current-source-build"
    assert verifier.report["image_identity"] == IMAGE
    assert verifier.built_identity == IMAGE


def test_del_05_backup_build_rejects_remote_context_before_acquisition(acquisition):
    verifier, docker, state = acquisition
    state.endpoint = "ssh://untrusted-host"
    with pytest.raises(transport.TransportError, match="requires_local_docker"):
        verifier.acquire_image(docker)
    assert len(state.calls) == 1
    assert not state.builds
    assert not verifier.build_attempted


def test_del_05_backup_build_preserves_existing_tag(acquisition):
    verifier, docker, state = acquisition
    state.images[verifier.build_tag] = (OTHER_IMAGE, {})
    with pytest.raises(gate.BackupVerificationError, match="build_tag_already_exists"):
        verifier.acquire_image(docker)
    verifier.cleanup()
    assert state.images[verifier.build_tag] == (OTHER_IMAGE, {})
    assert not state.builds
    assert not any(call[:2] == ["image", "rm"] for call, env in state.calls)


def test_del_05_backup_existing_image_never_builds_or_pulls(acquisition):
    _, docker, state = acquisition
    verifier = gate.BackupVerification("existing-backend:local")
    assert verifier.acquire_image(docker) == IMAGE
    assert verifier.report["image_acquisition"] == "existing-local"
    assert not state.builds
    assert all(call[0] != "pull" for call, env in state.calls)
    assert state.images == {"existing-backend:local": (IMAGE, {})}


def test_del_05_backup_build_cleanup_removes_only_exact_created_tags(acquisition):
    verifier, docker, state = acquisition
    identity = verifier.acquire_image(docker)
    verifier.tag_image(docker, identity, verifier.source_project)
    verifier.tag_image(docker, identity, verifier.restore_project)
    verifier.cleanup()
    assert state.images == {"existing-backend:local": (IMAGE, {})}
    assert [call[-1] for call, env in state.calls if call[:2] == ["image", "rm"]] == [
        f"{verifier.restore_project}-backend:local",
        f"{verifier.source_project}-backend:local",
        verifier.build_tag,
    ]
    assert not any("prune" in call or "--force" in call for call, env in state.calls)


@pytest.mark.parametrize("changed", ("owner", "identity"))
def test_del_05_backup_build_cleanup_preserves_changed_image(acquisition, changed):
    verifier, docker, state = acquisition
    verifier.acquire_image(docker)
    state.images[verifier.build_tag] = (
        OTHER_IMAGE if changed == "identity" else IMAGE,
        {transport.RUN_LABEL: "foreign" if changed == "owner" else verifier.operation},
    )
    with pytest.raises(gate.BackupVerificationError, match=r"built_image.*changed"):
        verifier.cleanup()
    assert verifier.build_tag in state.images
    assert not any(call[:2] == ["image", "rm"] for call, env in state.calls)


@pytest.mark.parametrize("failure", ("nonzero", "timeout"))
def test_del_05_backup_failed_build_cleans_only_labeled_output(acquisition, failure):
    verifier, docker, state = acquisition
    state.returncode = 1
    state.build_error = subprocess.TimeoutExpired("docker", 1) if failure == "timeout" else None
    with pytest.raises((gate.BackupVerificationError, subprocess.TimeoutExpired)):
        verifier.acquire_image(docker)
    verifier.cleanup()
    assert state.images == {"existing-backend:local": (IMAGE, {})}


@pytest.mark.parametrize("termination", ("signal", "deadline"))
def test_del_05_backup_gate_interruption_cleans_bundles_and_restores_handlers(
    monkeypatch, termination
):
    verifier = gate.BackupVerification("existing-backend:local")
    prior = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
    }
    directories = []
    cleanups = []

    def verify(directory):
        directories.append(directory)
        (directory / "private-fixture").write_bytes(b"fixture-not-a-real-secret")
        if termination == "signal":
            signal.raise_signal(signal.SIGTERM)
        else:
            time.sleep(1)

    def cleanup():
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
        assert 0 < signal.getitimer(signal.ITIMER_REAL)[0] <= gate.CLEANUP_TIMEOUT
        cleanups.append(True)

    monkeypatch.setattr(gate, "EXECUTION_TIMEOUT", 0.02)
    monkeypatch.setattr(verifier, "verify", verify)
    monkeypatch.setattr(verifier, "cleanup", cleanup)
    with pytest.raises(
        gate.BackupVerificationError, match=r"interrupted|execution_deadline_exceeded"
    ):
        verifier.run()
    assert cleanups == [True]
    assert all(not directory.exists() for directory in directories)
    assert {signum: signal.getsignal(signum) for signum in prior} == prior
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_del_05_backup_gate_aggregate_cleanup_deadline_removes_private_temp(monkeypatch):
    verifier = gate.BackupVerification("existing-backend:local")
    directories = []
    monkeypatch.setattr(verifier, "verify", lambda directory: directories.append(directory))
    monkeypatch.setattr(verifier, "cleanup", lambda: time.sleep(1))
    monkeypatch.setattr(gate, "CLEANUP_TIMEOUT", 0.02)
    with pytest.raises(gate.BackupVerificationError, match="cleanup_deadline_exceeded"):
        verifier.run()
    assert all(not directory.exists() for directory in directories)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_del_05_backup_gate_restores_prior_interval_timer(monkeypatch):
    verifier = gate.BackupVerification("existing-backend:local")
    monkeypatch.setattr(verifier, "verify", lambda directory: None)
    monkeypatch.setattr(verifier, "cleanup", lambda: None)
    prior = signal.setitimer(signal.ITIMER_REAL, 30, 2)
    try:
        verifier.run()
        remaining, interval = signal.getitimer(signal.ITIMER_REAL)
        assert 29 < remaining <= 30
        assert interval == 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, *prior)


def test_del_05_backup_gate_deadlines_leave_outer_timeout_margin():
    assert gate.EXECUTION_TIMEOUT == 420
    assert gate.CLEANUP_TIMEOUT == 150
    assert gate.EXECUTION_TIMEOUT + gate.CLEANUP_TIMEOUT < 600
