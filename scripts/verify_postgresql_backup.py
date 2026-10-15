"""Opt-in isolated PostgreSQL paired-backup round trip with exact owned cleanup.

--build builds both current-source Dockerfile targets on the pinned local daemon.
Explicit image arguments use already-local images only. No application network,
TCP PostgreSQL listener, provider, existing installation, or legacy volume is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

try:
    from . import postgresql_backup as transport
except ImportError:
    import postgresql_backup as transport

EXECUTION_TIMEOUT = 1200
CLEANUP_TIMEOUT = 150


class VerificationInterrupted(BaseException):
    """A deadline is not an ordinary cleanup error that may be aggregated."""


class VerificationDeadline:
    """One execution budget followed by one shared, non-renewable cleanup budget."""

    def __init__(self) -> None:
        self.execution_until: float | None = None
        self.cleanup_until: float | None = None

    def start(self) -> None:
        self.execution_until = time.monotonic() + EXECUTION_TIMEOUT
        signal.setitimer(signal.ITIMER_REAL, EXECUTION_TIMEOUT)

    def begin_cleanup(self) -> None:
        if self.cleanup_until is None:
            self.cleanup_until = time.monotonic() + CLEANUP_TIMEOUT
            signal.setitimer(signal.ITIMER_REAL, CLEANUP_TIMEOUT)
        self.check()

    def check(self) -> None:
        self.remaining()

    def remaining(self) -> float:
        until = self.cleanup_until if self.cleanup_until is not None else self.execution_until
        if until is None:
            return EXECUTION_TIMEOUT
        remaining = until - time.monotonic()
        if remaining <= 0:
            if self.cleanup_until is None:
                self.expired(signal.SIGALRM, None)
            raise VerificationInterrupted("postgresql_verification_cleanup_deadline")
        return remaining

    def expired(self, signum, frame) -> None:
        # Arm cleanup before unwinding through helper/restore finally blocks.
        # Outer cleanup shares this same budget; it must never renew it.
        self.begin_cleanup()
        raise VerificationInterrupted("postgresql_verification_deadline_or_interruption")


class BackupVerification:
    def __init__(self, image: str | None, postgres_image: str | None, *, build: bool) -> None:
        transport.require(
            (build and image is None and postgres_image is None)
            or (not build and bool(image) and bool(postgres_image)),
            "postgresql_verification_choose_build_or_images",
        )
        self.image, self.postgres_image, self.build = image, postgres_image, build
        self.token = uuid.uuid4().hex
        self.source_project = "marketing-agents-pg-backup-" + self.token[:16]
        self.restore_project = "marketing-agents-pg-restored-" + self.token[:16]
        self.transports: list[transport.DockerTransport] = []
        self.aliases: list[tuple[str, str]] = []
        self.build_tags: list[str] = []
        self.deadline: VerificationDeadline | None = None
        self.report: dict[str, object] = {
            "ok": False,
            "scope": "isolated-local-postgresql-backup-restore",
            "source_project": self.source_project,
            "restore_project": self.restore_project,
        }

    def tracked_transport_class(self):
        owner, original = self, transport.DockerTransport

        class TrackedTransport(original):
            def __init__(self, project):
                super().__init__(project)
                owner.transports.append(self)

            def command(self, arguments, **kwargs):
                if owner.deadline is not None:
                    owner.deadline.check()
                return super().command(arguments, **kwargs)

            def subprocess_timeout(self, default):
                if owner.deadline is not None:
                    return min(default, owner.deadline.remaining())
                return default

        return TrackedTransport

    def acquire(self, docker: transport.DockerTransport) -> tuple[str, str]:
        docker.local_daemon()
        if not self.build:
            assert self.image is not None and self.postgres_image is not None
            backend, postgres = docker.image_id(self.image), docker.image_id(self.postgres_image)
            # Removing our temporary alias must never delete a pre-existing
            # untagged image. Diagnostic mode requires an independent name.
            transport.require(
                bool(json.loads(docker.command(["image", "inspect", backend]))[0].get("RepoTags")),
                "postgresql_verification_existing_backend_tag_required",
            )
            return backend, postgres
        identities = []
        for target in ("runtime", "postgres"):
            tag = f"{self.source_project}-{target}-build:local"
            transport.require(
                not docker.command(
                    ["image", "ls", "--filter", f"reference={tag}", "--format", "{{.ID}}"]
                ).strip(),
                "postgresql_verification_build_tag_exists",
            )
            self.build_tags.append(tag)
            result = subprocess.run(
                [
                    "docker",
                    "build",
                    "--builder",
                    "default",
                    "--pull=false",
                    "--target",
                    target,
                    "--file",
                    str(transport.ROOT / "docker/api.Dockerfile"),
                    "--label",
                    f"{transport.RUN_LABEL}={self.token}",
                    "--tag",
                    tag,
                    str(transport.ROOT),
                ],
                cwd=transport.ROOT,
                env=docker.env,
                capture_output=True,
                timeout=docker.subprocess_timeout(EXECUTION_TIMEOUT),
                check=False,
            )
            transport.require(result.returncode == 0, "postgresql_verification_build_failed")
            value = json.loads(docker.command(["image", "inspect", tag]))[0]
            transport.require(
                value["Config"]["Labels"].get(transport.RUN_LABEL) == self.token,
                "postgresql_verification_build_ownership_invalid",
            )
            identities.append(docker.image_id(tag))
        return identities[0], identities[1]

    def tag(self, docker: transport.DockerTransport, image: str, project: str) -> None:
        alias = f"{project}-backend:local"
        transport.require(
            not docker.command(
                ["image", "ls", "--filter", f"reference={alias}", "--format", "{{.ID}}"]
            ).strip(),
            "postgresql_verification_alias_exists",
        )
        self.aliases.append((alias, image))
        docker.command(["image", "tag", image, alias])

    def verify(self, temporary: Path) -> None:
        self.report["stage"] = "acquire"
        source = transport.DockerTransport(self.source_project)
        backend, postgres = self.acquire(source)
        self.report.update(
            image_acquisition="current-source-build" if self.build else "existing-local",
            backend_identity=backend,
            postgres_identity=postgres,
        )
        self.tag(source, backend, self.source_project)
        self.tag(source, backend, self.restore_project)
        self.report["stage"] = "source-start"
        source.create_volumes()
        source.start_server(postgres)
        self.report["stage"] = "seed"
        source.helper(backend, "_seed")
        self.report["stage"] = "source-snapshot"
        before = json.loads(source.helper(backend, "_snapshot"))
        transport.require(
            set(before["counts"])
            == {
                "connector_action_receipts",
                "external_actions",
                "webhook_receipts",
                "schedule_occurrences",
            },
            "postgresql_verification_runtime_inventory_invalid",
        )
        bundle = temporary / "bundle"
        self.report["stage"] = "backup"
        transport.compose_backup(self.source_project, bundle)
        manifest = transport.validate_bundle(bundle)
        digest_before = {name: transport._checksum(bundle / name) for name in transport.FILES}
        self.report["stage"] = "restore"
        transport.compose_restore(self.restore_project, backend, postgres, bundle)
        target = next(
            item
            for item in self.transports
            if item.project == self.restore_project and item.created_volumes
        )
        target.start_server(postgres)
        self.report["stage"] = "restore-snapshot"
        after = json.loads(target.helper(backend, "_snapshot"))
        self.report["snapshot_comparison"] = {
            name: before.get(name) == after.get(name)
            for name in ("format_version", "key_fingerprint", "logical_database_sha256", "counts")
        }
        if before != after:
            self.report["table_differences"] = {
                table: [
                    part
                    for part in ("schema_sha256", "rows_sha256", "row_count")
                    if before["table_digests"].get(table, {}).get(part)
                    != after["table_digests"].get(table, {}).get(part)
                ]
                for table in sorted(set(before["table_digests"]) | set(after["table_digests"]))
                if before["table_digests"].get(table) != after["table_digests"].get(table)
            }
        transport.require(before == after, "postgresql_verification_roundtrip_mismatch")
        self.report["stage"] = "negative-controls"
        for operation in (
            lambda: transport.compose_backup(self.source_project, bundle),
            lambda: transport.compose_restore(self.restore_project, backend, postgres, bundle),
        ):
            try:
                operation()
            except transport.LocalBackupError:
                pass
            except transport.legacy.TransportError:
                pass
            else:
                raise transport.LocalBackupError("postgresql_verification_overwrite_allowed")
        transport.require(
            digest_before == {name: transport._checksum(bundle / name) for name in transport.FILES}
            and before == json.loads(source.helper(backend, "_snapshot"))
            and after == json.loads(target.helper(backend, "_snapshot")),
            "postgresql_verification_refusal_mutated_source",
        )
        corrupt = temporary / "corrupt"
        transport.copy_bundle(bundle, corrupt)
        with (corrupt / transport.DUMP_NAME).open("ab") as stream:
            stream.write(b"corruption")
        absent = "marketing-agents-pg-invalid-" + self.token[:16]
        try:
            transport.compose_restore(absent, backend, postgres, corrupt)
        except transport.LocalBackupError as error:
            transport.require(
                error.code == "postgresql_backup_checksum_invalid",
                "postgresql_verification_wrong_negative_control",
            )
        else:
            raise transport.LocalBackupError("postgresql_verification_corruption_accepted")
        invalid_target = next(item for item in self.transports if item.project == absent)
        invalid_target.require_absent_project()
        self.report.update(
            ok=True,
            stage="complete",
            schema_revision=manifest.schema_revision,
            postgresql_major=manifest.postgresql_major,
            logical_database_sha256=before["logical_database_sha256"],
            counts=before["counts"],
            checks=[
                "fresh-roundtrip-schema-and-all-rows",
                "paired-key-fingerprint",
                "existing-bundle-refused",
                "existing-project-refused",
                "corruption-refused-before-allocation",
                "refusals-preserve-state",
            ],
            # Fingerprint/digests are nonsecret evidence, never key bytes or rows.
            manifest_sha256=hashlib.sha256(
                (bundle / transport.MANIFEST_NAME).read_bytes()
            ).hexdigest(),
        )

    def cleanup(self) -> None:
        failed = False
        for docker in reversed(self.transports):
            for operation in (docker.stop_server, docker.cleanup_created_volumes):
                try:
                    operation()
                except Exception:
                    failed = True
        if not self.transports:
            transport.require(not failed, "postgresql_verification_cleanup_failed")
            return
        docker = self.transports[0]
        for tag, identity in reversed(self.aliases):
            try:
                found = docker.command(
                    ["image", "ls", "--filter", f"reference={tag}", "--format", "{{.ID}}"]
                )
                if found.strip():
                    transport.require(
                        docker.image_id(tag) == identity, "postgresql_verification_alias_changed"
                    )
                    if not self.build:
                        value = json.loads(docker.command(["image", "inspect", identity]))[0]
                        owned = {name for name, _ in self.aliases}
                        transport.require(
                            bool(set(value.get("RepoTags") or []) - owned),
                            "postgresql_verification_source_image_reference_changed",
                        )
                    docker.command(["image", "rm", tag])
            except Exception:
                failed = True
        for tag in reversed(self.build_tags):
            try:
                found = docker.command(
                    ["image", "ls", "--filter", f"reference={tag}", "--format", "{{.ID}}"]
                )
                if not found.strip():
                    continue
                value = json.loads(docker.command(["image", "inspect", tag]))[0]
                transport.require(
                    value["Config"]["Labels"].get(transport.RUN_LABEL) == self.token,
                    "postgresql_verification_build_ownership_changed",
                )
                docker.command(["image", "rm", tag])
            except Exception:
                failed = True
        transport.require(not failed, "postgresql_verification_cleanup_failed")
        self.report["cleanup"] = "owned-containers-volumes-image-tags-removed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--postgres-image")
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args(argv)
    verification = BackupVerification(args.image, args.postgres_image, build=args.build)
    deadline = verification.deadline = VerificationDeadline()
    original_transport = transport.DockerTransport
    transport.DockerTransport = verification.tracked_transport_class()
    previous = {
        number: signal.signal(number, deadline.expired)
        for number in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)
    }
    try:
        deadline.start()
        with tempfile.TemporaryDirectory(prefix="marketing-agents-pg-proof-") as directory:
            try:
                verification.verify(Path(directory))
            except (Exception, VerificationInterrupted) as error:
                verification.report.update(
                    ok=False,
                    error=error.code
                    if isinstance(error, transport.LocalBackupError)
                    else "postgresql_verification_failed",
                )
            finally:
                try:
                    deadline.begin_cleanup()
                    verification.cleanup()
                except (Exception, VerificationInterrupted):
                    verification.report.update(ok=False, cleanup="failed-inspect-owned-resources")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        transport.DockerTransport = original_transport
        for number, handler in previous.items():
            signal.signal(number, handler)
    print(json.dumps(verification.report, sort_keys=True))
    return 0 if verification.report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
