"""Paired native/Compose backups; restore only into new, explicitly scoped storage.

Compose transport uses completed, verified bundles over private tar streams. It
never copies a live database file, starts an application service, or pulls images.
The host runner is the repository's .venv Python. Guest helpers run as UID10001.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO

from marketing_agents.infrastructure.db.local_backup import (
    CHECKSUM_NAME,
    DATABASE_NAME,
    KEY_NAME,
    MANIFEST_NAME,
    _copy_private,
    _new_destination,
    _private_directory,
    _private_file,
    restore_local_installation,
)
from marketing_agents.infrastructure.db.local_installation import verify_local_installation

ROOT = Path(__file__).resolve().parents[1]
DATA_DIRECTORY = Path("/var/lib/marketing-agents/data")
SECRET_DIRECTORY = Path("/var/lib/marketing-agents/secrets")
SOCKET_DIRECTORY = Path("/var/run/marketing-agents")
DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIRECTORY / DATABASE_NAME}"
KEY_PATH = SECRET_DIRECTORY / "digest.key"
FILES = (DATABASE_NAME, KEY_NAME, MANIFEST_NAME, CHECKSUM_NAME)
RUN_LABEL = "org.marketing-agents.backup-transport"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


class TransportError(RuntimeError):
    pass


def require(condition: object, code: str) -> None:
    if not condition:
        raise TransportError(code)


def validate_project(project: str | None) -> str:
    require(
        project is not None
        and len(project) <= 64
        and re.fullmatch(r"marketing-agents-[a-z0-9][a-z0-9-]*", project)
        and not project.endswith("-"),
        "local_backup_project_invalid",
    )
    assert project is not None
    return project


def environment() -> dict[str, str]:
    retained = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG")
        if key in os.environ
    }
    return {**retained, "COMPOSE_DISABLE_ENV_FILE": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def paired_cli(*arguments: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "marketing_agents.workers.backup_cli", *arguments],
        capture_output=True,
        timeout=120,
        check=False,
    )
    require(result.returncode == 0, "local_backup_pair_verification_failed")
    require(len(result.stdout) <= 4096, "local_backup_result_invalid")
    require(json.loads(result.stdout).get("ok") is True, "local_backup_result_invalid")


def pack_bundle(bundle: Path, stream: BinaryIO) -> None:
    _private_directory(bundle)
    _private_directory(bundle / "secrets")
    require(
        {item.name for item in bundle.iterdir()}
        == {DATABASE_NAME, "secrets", MANIFEST_NAME, CHECKSUM_NAME},
        "local_backup_archive_invalid",
    )
    require(
        {item.name for item in (bundle / "secrets").iterdir()} == {"digest.key"},
        "local_backup_archive_invalid",
    )
    with tarfile.open(fileobj=stream, mode="w|") as archive:
        directory = tarfile.TarInfo("secrets")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o700
        archive.addfile(directory)
        for name in FILES:
            with _private_file(bundle / name) as source:
                item = tarfile.TarInfo(name)
                item.mode = 0o600
                item.size = os.fstat(source.fileno()).st_size
                require(item.size <= MAX_ARCHIVE_BYTES, "local_backup_transport_too_large")
                archive.addfile(item, source)


def unpack_bundle(archive_path: Path, destination: Path) -> None:
    """Prevalidate every archive member; never extract paths, links, or ownership."""
    require(archive_path.stat().st_size <= MAX_ARCHIVE_BYTES, "local_backup_transport_too_large")
    require(not os.path.lexists(destination), "local_backup_destination_exists")
    with tarfile.open(archive_path, "r:") as archive:
        members = archive.getmembers()
        require(
            len(members) == 5 and {member.name for member in members} == {*FILES, "secrets"},
            "local_backup_archive_invalid",
        )
        for member in members:
            require(
                (member.name == "secrets" and member.isdir() and member.mode == 0o700)
                or (member.name in FILES and member.isfile() and member.mode == 0o600),
                "local_backup_archive_invalid",
            )
        destination.mkdir(mode=0o700)
        (destination / "secrets").mkdir(mode=0o700)
        for member in members:
            if member.isdir():
                continue
            source = archive.extractfile(member)
            require(source is not None, "local_backup_archive_invalid")
            assert source is not None
            descriptor = os.open(
                destination / member.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with source, os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())


class DockerTransport:
    def __init__(self, project: str) -> None:
        self.project = validate_project(project)
        self.token = uuid.uuid4().hex
        self.env = environment()
        self.env["COMPOSE_PROJECT_NAME"] = self.project
        self.created_volumes: list[str] = []

    def command(
        self, arguments: list[str], *, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None
    ) -> bytes:
        result = subprocess.run(
            ["docker", *arguments],
            cwd=ROOT,
            env=self.env,
            stdin=stdin,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        require(result.returncode == 0, "local_backup_docker_command_failed")
        return result.stdout or b""

    def local_daemon(self) -> None:
        endpoint = None if self.env.get("DOCKER_CONTEXT") else self.env.get("DOCKER_HOST")
        if endpoint is None:
            endpoint = (
                self.command(["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
                .decode()
                .strip()
            )
        require(endpoint.startswith("unix:///"), "local_backup_requires_local_docker")
        # Pin the checked endpoint: later context selection changes cannot move
        # resource discovery, writes, or cleanup to another daemon.
        self.env.pop("DOCKER_CONTEXT", None)
        self.env["DOCKER_HOST"] = endpoint

    def source_image(self) -> str:
        config = json.loads(
            self.command(
                [
                    "compose",
                    "--env-file",
                    "/dev/null",
                    "--project-directory",
                    str(ROOT),
                    "--project-name",
                    self.project,
                    "--file",
                    str(ROOT / "compose.yaml"),
                    "config",
                    "--format",
                    "json",
                ]
            )
        )
        require(
            set(config["volumes"]) == {"data", "local-secrets", "api-socket"},
            "local_backup_compose_invalid",
        )
        for name in ("data", "local-secrets", "api-socket"):
            value = config["volumes"][name]
            require(
                value.get("name") == f"{self.project}_{name}" and not value.get("external"),
                "local_backup_compose_invalid",
            )
            if name != "api-socket":
                self.owned_volume(f"{self.project}_{name}")
        service = config["services"]["api"]
        require(
            service.get("user") == "10001:10001"
            and service["environment"].get("DATABASE_URL") == DATABASE_URL
            and service["environment"].get("MARKETING_AGENTS_DIGEST_KEY_PATH") == str(KEY_PATH),
            "local_backup_compose_invalid",
        )
        require(
            len(service.get("volumes", [])) == 3
            and {
                mount["target"]: (mount["type"], mount["source"], mount.get("read_only", False))
                for mount in service["volumes"]
            }
            == {
                str(DATA_DIRECTORY): ("volume", "data", False),
                str(SECRET_DIRECTORY): ("volume", "local-secrets", True),
                str(SOCKET_DIRECTORY): ("volume", "api-socket", False),
            },
            "local_backup_compose_invalid",
        )
        return self.image_id(service["image"])

    def image_id(self, image: str) -> str:
        require(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}", image),
            "local_backup_image_invalid",
        )
        identity = self.command(["image", "inspect", "--format", "{{.Id}}", image]).decode().strip()
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", identity), "local_backup_image_invalid")
        return identity

    def owned_volume(self, name: str, *, created: bool = False) -> None:
        require(
            name in {f"{self.project}_data", f"{self.project}_local-secrets"},
            "local_backup_volume_invalid",
        )
        inspected = json.loads(self.command(["volume", "inspect", name]))[0]
        labels = inspected.get("Labels") or {}
        require(
            inspected.get("Name") == name
            and labels.get("com.docker.compose.project") == self.project
            and labels.get("com.docker.compose.volume") == name.removeprefix(self.project + "_"),
            "local_backup_volume_ownership_invalid",
        )
        if created:
            require(labels.get(RUN_LABEL) == self.token, "local_backup_volume_ownership_invalid")

    def require_absent_project(self) -> None:
        for prefix in (["ps", "-a"], ["network", "ls"], ["volume", "ls"]):
            result = self.command(
                [
                    *prefix,
                    "--filter",
                    f"label=com.docker.compose.project={self.project}",
                    "--format",
                    "{{.ID}}" if prefix[0] != "volume" else "{{.Name}}",
                ]
            )
            require(not result.strip(), "local_backup_restore_project_exists")
        for suffix in ("data", "local-secrets", "api-socket"):
            result = self.command(
                [
                    "volume",
                    "ls",
                    "--filter",
                    f"name=^{self.project}_{suffix}$",
                    "--format",
                    "{{.Name}}",
                ]
            )
            require(not result.strip(), "local_backup_restore_volume_exists")

    def create_volumes(self) -> None:
        self.require_absent_project()
        for suffix in ("data", "local-secrets"):
            name = f"{self.project}_{suffix}"
            self.command(
                [
                    "volume",
                    "create",
                    "--label",
                    f"com.docker.compose.project={self.project}",
                    "--label",
                    f"com.docker.compose.volume={suffix}",
                    "--label",
                    f"{RUN_LABEL}={self.token}",
                    name,
                ]
            )
            self.owned_volume(name, created=True)
            self.created_volumes.append(name)

    def cleanup_created_volumes(self) -> None:
        for name in reversed(self.created_volumes):
            self.owned_volume(name, created=True)
            users = self.command(["ps", "-aq", "--filter", f"volume={name}"])
            require(not users.strip(), "local_backup_restore_volume_in_use")
            self.command(["volume", "rm", name])

    def container(
        self, image: str, operation: str, *, archive: BinaryIO, restore: bool = False
    ) -> None:
        name = "marketing-agents-transfer-" + uuid.uuid4().hex[:16]
        mounts = []
        if operation in {"_export", "_import"}:
            mounts = [
                "--mount",
                f"type=volume,source={self.project}_data,target={DATA_DIRECTORY}",
                "--mount",
                f"type=volume,source={self.project}_local-secrets,target={SECRET_DIRECTORY}"
                + ("" if restore else ",readonly"),
            ]
        arguments = [
            "run",
            "--rm",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            f"{RUN_LABEL}={self.token}",
            "--network",
            "none",
            "--user",
            "10001:10001",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:size=1536m,mode=1777",
            *mounts,
        ]
        if restore:
            arguments.append("--interactive")
        arguments.extend([image, "python", "/app/scripts/local_backup.py", operation])
        try:
            self.command(
                arguments, stdin=archive if restore else None, stdout=None if restore else archive
            )
        finally:
            existing = self.command(["ps", "-aq", "--filter", f"name=^/{name}$"])
            if existing.strip():
                inspected = json.loads(self.command(["inspect", name]))[0]
                require(
                    inspected["Config"]["Labels"].get(RUN_LABEL) == self.token,
                    "local_backup_container_ownership_invalid",
                )
                self.command(["rm", "--force", name])


def compose_backup(project: str, destination: Path) -> None:
    destination = _new_destination(destination)
    _private_directory(destination.parent)
    docker = DockerTransport(project)
    docker.local_daemon()
    image = docker.source_image()
    with tempfile.TemporaryDirectory(prefix="marketing-agents-backup-") as directory:
        temporary = Path(directory)
        archive_path = temporary / "transfer.tar"
        with archive_path.open("xb") as archive:
            archive_path.chmod(0o600)
            docker.container(image, "_export", archive=archive)
        unpack_bundle(archive_path, temporary / "incoming")
        asyncio.run(restore_local_installation(temporary / "incoming", destination))


def compose_restore(project: str, image: str, backup: Path) -> None:
    docker = DockerTransport(project)
    docker.local_daemon()
    docker.require_absent_project()
    identity = docker.image_id(image)
    with tempfile.TemporaryDirectory(prefix="marketing-agents-restore-") as directory:
        temporary = Path(directory)
        # Validate source and copied bytes before allocating any target volume.
        asyncio.run(restore_local_installation(backup, temporary / "verified"))
        archive_path = temporary / "transfer.tar"
        with archive_path.open("xb") as archive:
            archive_path.chmod(0o600)
            pack_bundle(temporary / "verified", archive)
        try:
            docker.create_volumes()
            with archive_path.open("rb") as archive:
                docker.container(identity, "_import", archive=archive, restore=True)
        except BaseException:
            docker.cleanup_created_volumes()
            raise


def guest(operation: str) -> None:
    require(os.getuid() == 10001, "local_backup_guest_uid_invalid")
    with tempfile.TemporaryDirectory(prefix="paired-transfer-") as directory:
        temporary = Path(directory)
        if operation == "_export":
            paired_cli(
                "backup",
                "--database-url",
                DATABASE_URL,
                "--key-path",
                str(KEY_PATH),
                "--destination",
                str(temporary / "bundle"),
            )
            pack_bundle(temporary / "bundle", sys.stdout.buffer)
        else:
            _private_directory(DATA_DIRECTORY)
            _private_directory(SECRET_DIRECTORY)
            require(
                not any(DATA_DIRECTORY.iterdir()) and not any(SECRET_DIRECTORY.iterdir()),
                "local_backup_restore_storage_not_empty",
            )
            archive_path = temporary / "transfer.tar"
            with archive_path.open("xb") as archive:
                archive_path.chmod(0o600)
                total = 0
                while block := sys.stdin.buffer.read(1024 * 1024):
                    total += len(block)
                    require(total <= MAX_ARCHIVE_BYTES, "local_backup_transport_too_large")
                    archive.write(block)
            unpack_bundle(archive_path, temporary / "incoming")
            paired_cli(
                "restore",
                "--backup",
                str(temporary / "incoming"),
                "--destination",
                str(temporary / "verified"),
            )
            # These are newly allocated, empty volumes. Exclusive file creation
            # preserves any unexpected existing target; no application is started.
            _copy_private(temporary / "verified" / DATABASE_NAME, DATA_DIRECTORY / DATABASE_NAME)
            _copy_private(temporary / "verified" / KEY_NAME, KEY_PATH)
            asyncio.run(verify_local_installation(DATABASE_URL, KEY_PATH))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("backup", "restore"):
        command = commands.add_parser(operation)
        command.add_argument("--mode", choices=("compose", "native"), default="compose")
        command.add_argument("--project")
        if operation == "backup":
            command.add_argument("--destination", type=Path, required=True)
            command.add_argument("--database-url")
            command.add_argument("--key-path", type=Path)
        else:
            command.add_argument("--backup", type=Path, required=True)
            command.add_argument("--destination", type=Path)
            command.add_argument("--image", default="marketing-agents-local-backend:local")
    commands.add_parser("_export")
    commands.add_parser("_import")
    args = parser.parse_args(argv)
    try:
        if args.operation.startswith("_"):
            guest(args.operation)
            return 0
        if args.mode == "native":
            arguments = [args.operation]
            if args.operation == "restore":
                require(args.destination is not None, "local_backup_destination_required")
                arguments.extend(["--backup", str(args.backup)])
            else:
                if args.database_url:
                    arguments.extend(["--database-url", args.database_url])
                if args.key_path:
                    arguments.extend(["--key-path", str(args.key_path)])
            paired_cli(*arguments, "--destination", str(args.destination))
        elif args.operation == "backup":
            require(not args.database_url and not args.key_path, "local_backup_compose_paths_fixed")
            compose_backup(args.project or "marketing-agents-local", args.destination)
        else:
            require(args.destination is None, "local_backup_compose_destination_is_project")
            compose_restore(validate_project(args.project), args.image, args.backup)
    except Exception:
        print(json.dumps({"ok": False, "code": "local_backup_transport_failed"}))
        return 1
    print(json.dumps({"ok": True, "code": f"local_{args.mode}_{args.operation}_complete"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
