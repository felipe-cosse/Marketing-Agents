"""DEL-05 transport boundaries: private archives, exact resources, and no service startup."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import tarfile
from types import SimpleNamespace

import pytest
from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.seed import seed_catalog
from marketing_agents.infrastructure.db import create_database_runtime
from marketing_agents.infrastructure.db.local_backup import (
    DATABASE_NAME,
    KEY_NAME,
    LocalBackupError,
    backup_local_installation,
)
from marketing_agents.infrastructure.db.local_installation import migrate_local_database
from marketing_agents.infrastructure.scheduling import CroniterRecurrenceCalculator
from marketing_agents.security.digest_key import DigestKeyError

from scripts import local_backup as transport
from scripts.verify_del_05_backup import BackupVerification, BackupVerificationError

PROJECT = "marketing-agents-del05-transport-test"
IMAGE = "sha256:" + "a" * 64


@pytest.fixture(scope="module")
def paired_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp("del05-transport")
    source = directory / "source"
    source.mkdir(mode=0o700)
    database, key = source / DATABASE_NAME, source / KEY_NAME

    async def prepare():
        url = f"sqlite+aiosqlite:///{database}"
        await migrate_local_database(url, key)
        runtime = create_database_runtime(url)
        try:
            await seed_catalog(
                compile_catalog(transport.ROOT / "catalog/v1"),
                runtime,
                CroniterRecurrenceCalculator(),
            )
        finally:
            await runtime.dispose()
        await backup_local_installation(url, key, directory / "bundle")

    asyncio.run(prepare())
    return directory / "bundle"


def bundle_bytes(bundle):
    stream = io.BytesIO()
    transport.pack_bundle(bundle, stream)
    return stream.getvalue()


class FakeDocker:
    def __init__(self, project=PROJECT):
        self.project = project
        self.volumes = {}
        self.calls = []
        self.archive = b""
        self.project_exists = False
        self.fail_import = False
        self.race_second_volume = False
        self.endpoint = b"unix:///var/run/docker.sock"

    def volume(self, suffix, token=None, project=None):
        name = f"{self.project}_{suffix}"
        labels = {
            "com.docker.compose.project": project or self.project,
            "com.docker.compose.volume": suffix,
        }
        if token:
            labels[transport.RUN_LABEL] = token
        self.volumes[name] = {"Name": name, "Labels": labels}

    def config(self):
        return {
            "volumes": {
                name: {"name": f"{self.project}_{name}"}
                for name in ("data", "local-secrets", "api-socket")
            },
            "services": {
                "api": {
                    "user": "10001:10001",
                    "image": f"{self.project}-backend:local",
                    "environment": {
                        "DATABASE_URL": transport.DATABASE_URL,
                        "MARKETING_AGENTS_DIGEST_KEY_PATH": str(transport.KEY_PATH),
                    },
                    "volumes": [
                        {
                            "type": "volume",
                            "source": "data",
                            "target": str(transport.DATA_DIRECTORY),
                        },
                        {
                            "type": "volume",
                            "source": "local-secrets",
                            "read_only": True,
                            "target": str(transport.SECRET_DIRECTORY),
                        },
                        {
                            "type": "volume",
                            "source": "api-socket",
                            "target": str(transport.SOCKET_DIRECTORY),
                        },
                    ],
                }
            },
        }

    def command(self, instance, arguments, *, stdin=None, stdout=None):
        self.calls.append(arguments)
        assert instance.env["COMPOSE_PROJECT_NAME"] == self.project
        if arguments[:2] == ["context", "inspect"]:
            return self.endpoint
        if arguments[0] == "compose":
            assert arguments[arguments.index("--env-file") + 1] == "/dev/null"
            return json.dumps(self.config()).encode()
        if arguments[:2] == ["image", "inspect"]:
            return IMAGE.encode()
        if arguments[0] in {"ps", "network"}:
            if any("label=com.docker.compose.project=" in arg for arg in arguments):
                return b"existing-resource" if self.project_exists else b""
            return b""
        if arguments[:2] == ["volume", "ls"]:
            query = arguments[arguments.index("--filter") + 1]
            if query.startswith("name=^"):
                return query[6:-1].encode() if query[6:-1] in self.volumes else b""
            names = [
                name
                for name, value in self.volumes.items()
                if value["Labels"].get("com.docker.compose.project") == self.project
            ]
            return "\n".join(names).encode()
        if arguments[:2] == ["volume", "inspect"]:
            return json.dumps([self.volumes[arguments[-1]]]).encode()
        if arguments[:2] == ["volume", "create"]:
            suffix = arguments[-1].removeprefix(self.project + "_")
            if self.race_second_volume and suffix == "local-secrets":
                self.volume(suffix, project="unrelated-owner")
            else:
                self.volume(suffix, instance.token)
            return arguments[-1].encode()
        if arguments[:2] == ["volume", "rm"]:
            del self.volumes[arguments[-1]]
            return b""
        if arguments[0] == "run":
            assert arguments[arguments.index("--network") + 1] == "none"
            assert arguments[arguments.index("--pull") + 1] == "never"
            assert arguments[arguments.index("--user") + 1] == "10001:10001"
            assert "--read-only" in arguments and "ALL" in arguments
            assert IMAGE in arguments
            assert not any("type=bind" in arg for arg in arguments)
            assert not any("api-socket" in arg for arg in arguments)
            if arguments[-1] == "_export":
                assert stdout is not None and stdin is None
                stdout.write(self.archive)
            else:
                assert stdin is not None and stdout is None
                assert stdin.read(512)
                if self.fail_import:
                    raise transport.TransportError("injected_import_failure")
            return b""
        raise AssertionError(f"Unexpected Docker operation: {arguments[:2]}")


@pytest.fixture
def docker(monkeypatch):
    value = FakeDocker()
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(
        transport.DockerTransport,
        "command",
        lambda instance, arguments, **kwargs: value.command(instance, arguments, **kwargs),
    )
    return value


@pytest.mark.parametrize(
    "project", (None, "", "default", "marketing-agents-", "../outside", "marketing-agents-a;true")
)
def test_del_05_transport_rejects_unsafe_projects(project):
    with pytest.raises(transport.TransportError, match="project_invalid"):
        transport.validate_project(project)


def test_del_05_transport_rejects_remote_selected_context_even_with_local_host(docker, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///safe/local.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-selected")
    docker.endpoint = b"ssh://remote.invalid"
    with pytest.raises(transport.TransportError, match="requires_local_docker"):
        transport.DockerTransport(PROJECT).local_daemon()
    assert len(docker.calls) == 1


def test_del_05_transport_pins_effective_local_context_for_later_commands(docker, monkeypatch):
    monkeypatch.setenv("DOCKER_HOST", "unix:///not-the-selected.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "selected-local-context")
    docker.endpoint = b"unix:///selected/local.sock"
    instance = transport.DockerTransport(PROJECT)
    instance.local_daemon()
    assert instance.env["DOCKER_HOST"] == "unix:///selected/local.sock"
    assert "DOCKER_CONTEXT" not in instance.env
    docker.endpoint = b"ssh://a-later-context-change.invalid"
    assert instance.image_id("existing:local") == IMAGE
    assert instance.env["DOCKER_HOST"] == "unix:///selected/local.sock"


@pytest.mark.parametrize(
    "problem",
    ("escape", "symlink", "hardlink", "key_permissions", "directory_permissions", "duplicate"),
)
def test_del_05_transport_prevalidates_archive_before_any_extraction(tmp_path, problem):
    path = tmp_path / "invalid.tar"
    with tarfile.open(path, "w") as archive:
        for name in ("secrets", *transport.FILES):
            item = tarfile.TarInfo(name)
            item.type = tarfile.DIRTYPE if name == "secrets" else tarfile.REGTYPE
            item.mode = 0o700 if name == "secrets" else 0o600
            item.size = 0
            if name == KEY_NAME:
                if problem == "escape":
                    item.name = "../outside"
                elif problem in {"symlink", "hardlink"}:
                    item.type = tarfile.SYMTYPE if problem == "symlink" else tarfile.LNKTYPE
                    item.linkname = "/outside"
                elif problem == "key_permissions":
                    item.mode = 0o644
                elif problem == "duplicate":
                    item.name = DATABASE_NAME
            if name == "secrets" and problem == "directory_permissions":
                item.mode = 0o755
            archive.addfile(item)
    destination = tmp_path / "untouched"
    with pytest.raises(transport.TransportError, match="archive_invalid"):
        transport.unpack_bundle(path, destination)
    assert not destination.exists()
    assert not (tmp_path.parent / "outside").exists()


def test_del_05_transport_backup_publishes_only_complete_verified_bundle(
    docker, paired_bundle, tmp_path
):
    docker.volume("data")
    docker.volume("local-secrets")
    docker.archive = bundle_bytes(paired_bundle)
    destination = tmp_path / "published"
    before = dict(docker.volumes)
    transport.compose_backup(PROJECT, destination)
    assert (destination / KEY_NAME).read_bytes() == (paired_bundle / KEY_NAME).read_bytes()
    assert (destination / DATABASE_NAME).read_bytes() == (
        paired_bundle / DATABASE_NAME
    ).read_bytes()
    assert docker.volumes == before
    assert not any(call[:2] == ["volume", "create"] for call in docker.calls)
    assert not any("cp" in call or "up" in call or "down" in call for call in docker.calls)


def test_del_05_transport_backup_existing_destination_is_untouched_before_docker(docker, tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir(mode=0o700)
    (destination / "sentinel").write_text("active storage")
    with pytest.raises(LocalBackupError, match="destination_exists"):
        transport.compose_backup(PROJECT, destination)
    assert (destination / "sentinel").read_text() == "active storage"
    assert docker.calls == []


@pytest.mark.parametrize("problem", ("missing_key", "permissive_key", "database_corrupt"))
def test_del_05_transport_invalid_restore_creates_no_volumes_or_containers(
    docker, paired_bundle, tmp_path, problem
):
    bundle = tmp_path / "broken"
    shutil.copytree(paired_bundle, bundle)
    if problem == "missing_key":
        (bundle / KEY_NAME).unlink()
    elif problem == "permissive_key":
        (bundle / KEY_NAME).chmod(0o644)
    else:
        with (bundle / DATABASE_NAME).open("ab") as stream:
            stream.write(b"damaged")
    with pytest.raises((LocalBackupError, DigestKeyError, FileNotFoundError)):
        transport.compose_restore(PROJECT, "existing-backend:local", bundle)
    assert docker.volumes == {}
    assert not any(call[0] == "run" or call[:2] == ["volume", "create"] for call in docker.calls)


@pytest.mark.parametrize("existing", ("project", "foreign_volume", "foreign_ipc"))
def test_del_05_transport_restore_rejects_existing_resources(docker, paired_bundle, existing):
    if existing == "project":
        docker.project_exists = True
    else:
        docker.volume(
            "api-socket" if existing == "foreign_ipc" else "data", project="unrelated-owner"
        )
    before = dict(docker.volumes)
    with pytest.raises(transport.TransportError, match="exists"):
        transport.compose_restore(PROJECT, "existing-backend:local", paired_bundle)
    assert docker.volumes == before
    assert not any(call[0] == "run" or call[:2] == ["volume", "create"] for call in docker.calls)


def test_del_05_transport_restore_creates_only_labeled_new_pair_and_no_application(
    docker, paired_bundle
):
    transport.compose_restore(PROJECT, "existing-backend:local", paired_bundle)
    assert set(docker.volumes) == {f"{PROJECT}_data", f"{PROJECT}_local-secrets"}
    assert all(value["Labels"].get(transport.RUN_LABEL) for value in docker.volumes.values())
    runs = [call for call in docker.calls if call[0] == "run"]
    assert len(runs) == 1 and runs[0][-1] == "_import"
    assert not any(
        "up" in call or "start" in call or "cp" in call or "prune" in call for call in docker.calls
    )


def test_del_05_transport_import_failure_cleans_only_new_owned_unused_volumes(
    docker, paired_bundle
):
    docker.fail_import = True
    with pytest.raises(transport.TransportError, match="injected_import_failure"):
        transport.compose_restore(PROJECT, "existing-backend:local", paired_bundle)
    assert docker.volumes == {}
    removed = [call[-1] for call in docker.calls if call[:2] == ["volume", "rm"]]
    assert removed == [f"{PROJECT}_local-secrets", f"{PROJECT}_data"]


def test_del_05_transport_creation_race_preserves_foreign_volume(docker, paired_bundle):
    docker.race_second_volume = True
    with pytest.raises(transport.TransportError, match="ownership_invalid"):
        transport.compose_restore(PROJECT, "existing-backend:local", paired_bundle)
    assert set(docker.volumes) == {f"{PROJECT}_local-secrets"}
    assert (
        docker.volumes[f"{PROJECT}_local-secrets"]["Labels"]["com.docker.compose.project"]
        == "unrelated-owner"
    )
    assert [call[-1] for call in docker.calls if call[:2] == ["volume", "rm"]] == [
        f"{PROJECT}_data"
    ]


def test_del_05_transport_native_wrapper_restores_pair_and_sanitizes_errors(
    paired_bundle, tmp_path, capsys, monkeypatch
):
    destination = tmp_path / "native-restored"
    assert (
        transport.main(
            [
                "restore",
                "--mode",
                "native",
                "--backup",
                str(paired_bundle),
                "--destination",
                str(destination),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "code": "local_native_restore_complete",
    }
    assert (destination / KEY_NAME).read_bytes() == (paired_bundle / KEY_NAME).read_bytes()

    def fail(*arguments):
        raise RuntimeError("secret-must-not-appear")

    monkeypatch.setattr(transport, "paired_cli", fail)
    assert (
        transport.main(
            [
                "restore",
                "--mode",
                "native",
                "--backup",
                str(paired_bundle),
                "--destination",
                str(tmp_path / "not-created"),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert "secret-must-not-appear" not in output.out + output.err
    assert json.loads(output.out) == {"ok": False, "code": "local_backup_transport_failed"}


def test_del_05_transport_native_backup_forwards_explicit_pair(paired_bundle, tmp_path, capsys):
    destination = tmp_path / "native-backup"
    assert (
        transport.main(
            [
                "backup",
                "--mode",
                "native",
                "--destination",
                str(destination),
                "--database-url",
                f"sqlite+aiosqlite:///{paired_bundle / DATABASE_NAME}",
                "--key-path",
                str(paired_bundle / KEY_NAME),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "code": "local_native_backup_complete",
    }
    assert (destination / KEY_NAME).read_bytes() == (paired_bundle / KEY_NAME).read_bytes()


def test_del_05_live_gate_cleanup_refuses_changed_container_ownership():
    verifier = BackupVerification("existing-backend:local")
    calls = []

    def command(arguments):
        calls.append(arguments)
        if arguments[0] == "ps":
            return b"container-id"
        if arguments[0] == "inspect":
            return json.dumps([{"Config": {"Labels": {transport.RUN_LABEL: "foreign"}}}]).encode()
        raise AssertionError("Cleanup must stop before mutation")

    docker = SimpleNamespace(command=command, token="owned")
    verifier.containers = [(docker, "marketing-agents-transfer-0123456789abcdef")]
    with pytest.raises(BackupVerificationError, match="container_owner_changed"):
        verifier.cleanup()
    assert not any(call[0] == "rm" for call in calls)


def test_del_05_live_gate_cleanup_refuses_changed_image_alias():
    verifier = BackupVerification("existing-backend:local")
    alias = f"{verifier.source_project}-backend:local"
    calls = []
    docker = SimpleNamespace(
        created_volumes=[],
        image_id=lambda image: IMAGE if image == verifier.image else "sha256:" + "b" * 64,
        command=lambda arguments: calls.append(arguments),
    )
    verifier.transports = [docker]
    verifier.aliases = [(alias, IMAGE)]
    with pytest.raises(BackupVerificationError, match="image_alias_changed"):
        verifier.cleanup()
    assert calls == []
