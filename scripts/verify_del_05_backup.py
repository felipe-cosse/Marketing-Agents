"""Opt-in real Docker backup/restore gate using current source or an existing image.

Every application-volume name belongs to this invocation. No application service,
application network, provider access, or existing installation is needed. --image
never pulls; --build explicitly permits the Dockerfile's locked acquisition on the
local daemon. Secret-bearing bundles stay in an owner-only temporary directory
and are removed before return.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from marketing_agents.infrastructure.db.local_backup import (
    DATABASE_NAME,
    KEY_NAME,
    LocalBackupError,
    _validate_bundle,
)

try:
    from . import local_backup as transport
except ImportError:
    import local_backup as transport

EXECUTION_TIMEOUT = 420
CLEANUP_TIMEOUT = 150


class BackupVerificationError(RuntimeError):
    pass


def interrupted(signum, frame) -> None:
    raise BackupVerificationError("verification_interrupted")


def execution_expired(signum, frame) -> None:
    raise BackupVerificationError("verification_execution_deadline_exceeded")


def cleanup_expired(signum, frame) -> None:
    raise BackupVerificationError("verification_cleanup_deadline_exceeded")


def require(condition: object, code: str) -> None:
    if not condition:
        raise BackupVerificationError(code)


def snapshot_rows(database: Path) -> dict[str, tuple[tuple, ...]]:
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {
            name: tuple(sorted(connection.execute(f'SELECT * FROM "{name}"'), key=repr))
            for (name,) in names
        }
    finally:
        connection.close()


def bundle_hashes(bundle: Path) -> dict[str, str]:
    return {
        name: hashlib.sha256((bundle / name).read_bytes()).hexdigest() for name in transport.FILES
    }


class BackupVerification:
    def __init__(self, image: str | None = None, *, build: bool = False) -> None:
        require(bool(image) != build, "verification_choose_image_or_build")
        self.image = image
        self.build = build
        self.operation = uuid.uuid4().hex
        self.source_project = "marketing-agents-del05-backup-" + self.operation[:16]
        self.restore_project = "marketing-agents-del05-restore-" + self.operation[:16]
        self.transports: list[transport.DockerTransport] = []
        self.containers: list[tuple[transport.DockerTransport, str]] = []
        self.aliases: list[tuple[str, str]] = []
        self.commands: list[list[str]] = []
        self.build_tag = f"{self.source_project}-build:local"
        self.build_attempted = False
        self.built_identity: str | None = None
        self.report: dict[str, object] = {
            "ok": False,
            "scope": "isolated-local-sqlite-backup-restore",
            "source_project": self.source_project,
            "restore_project": self.restore_project,
        }

    def tracked_transport_class(self):
        owner = self
        original = transport.DockerTransport

        class TrackedTransport(original):
            def __init__(self, project):
                super().__init__(project)
                owner.transports.append(self)

            def command(self, arguments, **kwargs):
                owner.commands.append(list(arguments))
                if arguments and arguments[0] == "run":
                    name = arguments[arguments.index("--name") + 1]
                    owner.containers.append((self, name))
                return super().command(arguments, **kwargs)

        return TrackedTransport

    def verify_build_ownership(self, docker: transport.DockerTransport) -> str:
        inspected = json.loads(docker.command(["image", "inspect", self.build_tag]))
        require(
            isinstance(inspected, list) and len(inspected) == 1,
            "verification_built_image_inspection_invalid",
        )
        value = inspected[0]
        labels = value.get("Config", {}).get("Labels") or {}
        identity = value.get("Id", "")
        require(
            isinstance(labels, dict) and labels.get(transport.RUN_LABEL) == self.operation,
            "verification_built_image_owner_changed",
        )
        require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", identity),
            "verification_built_image_identity_invalid",
        )
        if self.built_identity is not None:
            require(
                identity == self.built_identity,
                "verification_built_image_changed",
            )
        # Identity and ownership come from one inspect result, not two mutable
        # tag lookups. Every subsequent helper uses this immutable identity.
        return identity

    def acquire_image(self, docker: transport.DockerTransport) -> str:
        # Resolve the effective context before any acquisition, then pin its
        # local socket. An inherited Buildx selection must not move the build.
        docker.local_daemon()
        if not self.build:
            assert self.image is not None
            identity = docker.image_id(self.image)
            self.report.update(image_acquisition="existing-local", image_identity=identity)
            return identity
        existing = docker.command(
            ["image", "ls", "--filter", f"reference={self.build_tag}", "--format", "{{.ID}}"]
        )
        require(not existing.strip(), "verification_build_tag_already_exists")
        arguments = [
            "build",
            "--builder",
            "default",
            "--pull=false",
            "--target",
            "runtime",
            "--file",
            str(transport.ROOT / "docker/api.Dockerfile"),
            "--label",
            f"{transport.RUN_LABEL}={self.operation}",
            "--tag",
            self.build_tag,
            str(transport.ROOT),
        ]
        self.commands.append(arguments)
        self.build_attempted = True
        result = subprocess.run(
            ["docker", *arguments],
            cwd=transport.ROOT,
            env=docker.env,
            capture_output=True,
            timeout=EXECUTION_TIMEOUT,
            check=False,
        )
        require(result.returncode == 0, "verification_source_build_failed")
        self.built_identity = self.verify_build_ownership(docker)
        self.report.update(
            image_acquisition="current-source-build", image_identity=self.built_identity
        )
        return self.built_identity

    def tag_image(self, docker: transport.DockerTransport, image: str, project: str) -> None:
        alias = f"{project}-backend:local"
        existing = docker.command(
            ["image", "ls", "--filter", f"reference={alias}", "--format", "{{.ID}}"]
        )
        require(not existing.strip(), "verification_image_alias_already_exists")
        docker.command(["image", "tag", image, alias])
        self.aliases.append((alias, image))
        require(docker.image_id(alias) == image, "verification_image_alias_changed")

    def run_helper(
        self, docker: transport.DockerTransport, image: str, command: list[str]
    ) -> bytes:
        name = "marketing-agents-backup-check-" + uuid.uuid4().hex[:16]
        return docker.command(
            [
                "run",
                "--rm",
                "--pull",
                "never",
                "--name",
                name,
                "--label",
                f"{transport.RUN_LABEL}={docker.token}",
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
                "/tmp:size=64m,mode=1777",
                "--mount",
                f"type=volume,source={docker.project}_data,target={transport.DATA_DIRECTORY}",
                "--mount",
                f"type=volume,source={docker.project}_local-secrets,target={transport.SECRET_DIRECTORY}",
                image,
                *command,
            ]
        )

    def verify_pair(self, docker: transport.DockerTransport, image: str) -> None:
        code = "\n".join(
            [
                "import asyncio, os, stat",
                "from pathlib import Path",
                "from marketing_agents.infrastructure.db.local_installation "
                "import verify_local_installation",
                f"database = Path({str(transport.DATA_DIRECTORY / DATABASE_NAME)!r})",
                f"key = Path({str(transport.KEY_PATH)!r})",
                "assert os.getuid() == 10001",
                "assert all(p.stat().st_uid == 10001 "
                "for p in (database, key, database.parent, key.parent))",
                "assert all(stat.S_IMODE(p.stat().st_mode) == 0o700 "
                "for p in (database.parent, key.parent))",
                "assert stat.S_IMODE(key.stat().st_mode) == 0o600",
                f"asyncio.run(verify_local_installation({transport.DATABASE_URL!r}, key))",
            ]
        )
        self.run_helper(docker, image, ["python", "-c", code])

    def verify(self, temporary: Path) -> None:
        source = transport.DockerTransport(self.source_project)
        identity = self.acquire_image(source)
        target = transport.DockerTransport(self.restore_project)
        target.local_daemon()
        source.require_absent_project()
        target.require_absent_project()
        self.tag_image(source, identity, self.source_project)
        self.tag_image(source, identity, self.restore_project)
        source.create_volumes()
        self.run_helper(source, identity, ["/bin/sh", "/app/scripts/migrate-and-seed.sh"])
        self.verify_pair(source, identity)

        first = temporary / "first-backup"
        restored = temporary / "restored-backup"
        after_refusal = temporary / "after-refusal"
        transport.compose_backup(self.source_project, first)
        initial = asyncio.run(_validate_bundle(first))
        original_rows = snapshot_rows(first / DATABASE_NAME)
        require(len(original_rows["agent_instances"]) == 43, "verification_catalog_count_invalid")
        require(
            len(original_rows["agent_instance_configs"]) == 43, "verification_config_count_invalid"
        )
        transport.compose_restore(self.restore_project, identity, first)
        self.verify_pair(target, identity)
        transport.compose_backup(self.restore_project, restored)
        recovered = asyncio.run(_validate_bundle(restored))
        require(
            snapshot_rows(restored / DATABASE_NAME) == original_rows, "verification_rows_changed"
        )
        require(
            initial.key_fingerprint == recovered.key_fingerprint
            and initial.catalog_hash == recovered.catalog_hash
            and initial.schema_revision == recovered.schema_revision
            and (first / KEY_NAME).read_bytes() == (restored / KEY_NAME).read_bytes(),
            "verification_pair_identity_changed",
        )

        before = bundle_hashes(first)
        try:
            transport.compose_backup(self.source_project, first)
        except LocalBackupError as exc:
            require(
                exc.code == "local_backup_destination_exists", "verification_wrong_backup_refusal"
            )
        else:
            raise BackupVerificationError("verification_backup_overwrote_existing_bundle")
        require(bundle_hashes(first) == before, "verification_existing_bundle_changed")
        try:
            transport.compose_restore(self.restore_project, identity, first)
        except transport.TransportError as exc:
            require(
                str(exc) == "local_backup_restore_project_exists",
                "verification_wrong_restore_refusal",
            )
        else:
            raise BackupVerificationError("verification_restore_overwrote_existing_project")
        transport.compose_backup(self.restore_project, after_refusal)
        require(
            snapshot_rows(after_refusal / DATABASE_NAME) == original_rows,
            "verification_refusal_changed_rows",
        )
        require(
            (after_refusal / KEY_NAME).read_bytes() == (first / KEY_NAME).read_bytes(),
            "verification_refusal_changed_key",
        )
        for project in (self.source_project, self.restore_project):
            require(
                not source.command(
                    ["ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]
                ).strip(),
                "verification_started_application_container",
            )
            require(
                not source.command(
                    [
                        "volume",
                        "ls",
                        "--filter",
                        f"name=^{project}_api-socket$",
                        "--format",
                        "{{.Name}}",
                    ]
                ).strip(),
                "verification_copied_ipc_storage",
            )
        self.report.update(
            {
                "ok": True,
                "schema_revision": initial.schema_revision,
                "table_count_including_alembic": len(original_rows),
                "catalog_instances": 43,
                "instance_configurations": 43,
                "all_rows_preserved": True,
                "key_identity_preserved": True,
                "uid_10001_pair_verified": True,
                "overwrite_refused_without_changes": True,
                "application_services_started": False,
                "ipc_storage_copied": False,
            }
        )

    def cleanup(self) -> None:
        for docker, name in reversed(self.containers):
            require(
                re.fullmatch(r"marketing-agents-(?:backup-check|transfer)-[0-9a-f]{16}", name),
                "verification_container_name_invalid",
            )
            existing = docker.command(["ps", "-aq", "--filter", f"name=^/{name}$"])
            if existing.strip():
                value = json.loads(docker.command(["inspect", name]))[0]
                require(
                    value["Config"]["Labels"].get(transport.RUN_LABEL) == docker.token,
                    "verification_container_owner_changed",
                )
                docker.command(["rm", "--force", name])
        for docker in reversed(self.transports):
            if not docker.created_volumes:
                continue
            remaining = []
            for name in docker.created_volumes:
                exists = docker.command(
                    ["volume", "ls", "--filter", f"name=^{name}$", "--format", "{{.Name}}"]
                )
                if exists.strip():
                    remaining.append(name)
            docker.created_volumes = remaining
            docker.cleanup_created_volumes()
        if self.transports:
            docker = self.transports[0]
            for alias, identity in reversed(self.aliases):
                require(
                    alias
                    in {
                        f"{self.source_project}-backend:local",
                        f"{self.restore_project}-backend:local",
                    },
                    "verification_image_alias_unsafe",
                )
                if self.image is not None:
                    require(
                        docker.image_id(self.image) == identity,
                        "verification_original_image_changed",
                    )
                require(docker.image_id(alias) == identity, "verification_image_alias_changed")
                docker.command(["image", "rm", alias])
            if self.build_attempted:
                require(
                    self.build_tag == f"{self.source_project}-build:local",
                    "verification_build_tag_unsafe",
                )
                exists = docker.command(
                    [
                        "image",
                        "ls",
                        "--filter",
                        f"reference={self.build_tag}",
                        "--format",
                        "{{.ID}}",
                    ]
                )
                if exists.strip():
                    self.verify_build_ownership(docker)
                    docker.command(["image", "rm", self.build_tag])
            for project in (self.source_project, self.restore_project):
                verifier = transport.DockerTransport(project)
                verifier.local_daemon()
                verifier.require_absent_project()
        self.report["cleanup_complete"] = True

    def run(self) -> dict[str, object]:
        started = time.monotonic()
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        previous_handlers = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
        }
        original = transport.DockerTransport
        transport.DockerTransport = self.tracked_transport_class()
        try:
            signal.signal(signal.SIGINT, interrupted)
            signal.signal(signal.SIGTERM, interrupted)
            signal.signal(signal.SIGALRM, execution_expired)
            signal.setitimer(signal.ITIMER_REAL, EXECUTION_TIMEOUT)
            with tempfile.TemporaryDirectory(prefix="marketing-agents-live-backup-") as directory:
                temporary = Path(directory)
                require(temporary.stat().st_uid == os.getuid(), "verification_temp_owner_invalid")
                try:
                    self.verify(temporary)
                finally:
                    # Finish bounded, ownership-checked cleanup on cancellation;
                    # a repeated SIGINT/SIGTERM must not abandon secret bundles.
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        signal.signal(signum, signal.SIG_IGN)
                    signal.signal(signal.SIGALRM, cleanup_expired)
                    signal.setitimer(signal.ITIMER_REAL, CLEANUP_TIMEOUT)
                    self.cleanup()
            self.report["private_bundles_removed"] = True
            return self.report
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            transport.DockerTransport = original
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            if previous_timer[0] > 0:
                remaining = max(0.001, previous_timer[0] - (time.monotonic() - started))
                signal.setitimer(signal.ITIMER_REAL, remaining, previous_timer[1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    acquisition = parser.add_mutually_exclusive_group(required=True)
    acquisition.add_argument("--image", help="Existing local backend image; never pulled")
    acquisition.add_argument(
        "--build", action="store_true", help="Build current runtime source on the local daemon"
    )
    args = parser.parse_args(argv)
    verifier = BackupVerification(args.image, build=args.build)
    try:
        result = verifier.run()
    except Exception as exc:
        result = {
            **verifier.report,
            "ok": False,
            "code": str(exc)
            if isinstance(exc, BackupVerificationError)
            else "backup_verification_failed",
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
