"""DEL-10: PostgreSQL pair format, snapshot lifetime and exact local transport controls."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import signal
import subprocess
import tarfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import postgresql_backup as backup
from scripts import verify_postgresql_backup as verifier
from scripts.verify_postgresql_backup import (
    BackupVerification,
    VerificationDeadline,
    VerificationInterrupted,
)

URL = backup.DATABASE_URL
IMAGE = "sha256:" + "a" * 64
PROJECT = "marketing-agents-pg-unit"


def manifest_payload(key: bytes, dump: bytes = b"PGDMPfixture") -> dict:
    snapshot = json.dumps({"manifest": {"content_version": "v1"}})
    return {
        "format_version": 1,
        "snapshot_method": "postgresql-exported-snapshot-custom-v1",
        "secret_bearing": True,
        "postgresql_major": 17,
        "schema_revision": backup.HEAD_REVISION,
        "key_format_version": 1,
        "key_fingerprint": backup.digest_key_fingerprint(backup._decode_key(key)),
        "catalog_version": "v1",
        "catalog_hash": "catalog-sha256-v1:" + hashlib.sha256(snapshot.encode()).hexdigest(),
        "database_sha256": hashlib.sha256(dump).hexdigest(),
        "key_sha256": hashlib.sha256(key).hexdigest(),
    }


def write_manifest(bundle: Path, payload: dict) -> None:
    for name in (backup.MANIFEST_NAME, backup.CHECKSUM_NAME):
        (bundle / name).unlink(missing_ok=True)
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode()
    backup._write_private(bundle / backup.MANIFEST_NAME, encoded)
    backup._write_private(
        bundle / backup.CHECKSUM_NAME, (hashlib.sha256(encoded).hexdigest() + "\n").encode()
    )


@pytest.fixture
def bundle(tmp_path):
    result = tmp_path / "bundle"
    result.mkdir(mode=0o700)
    (result / "secrets").mkdir(mode=0o700)
    key = base64.urlsafe_b64encode(b"a" * 32) + b"\n"
    backup._write_private(result / backup.KEY_NAME, key)
    backup._write_private(result / backup.DUMP_NAME, b"PGDMPfixture")
    write_manifest(result, manifest_payload(key))
    return result


def test_private_bundle_roundtrip_preserves_pair_and_exclusive_destination(bundle, tmp_path):
    original = backup.validate_bundle(bundle)
    target = tmp_path / "copy"
    assert backup.copy_bundle(bundle, target) == original
    assert backup.validate_bundle(target) == original
    assert (target / backup.KEY_NAME).read_bytes() == (bundle / backup.KEY_NAME).read_bytes()
    with pytest.raises(backup.LocalBackupError, match="destination_exists"):
        backup.copy_bundle(bundle, target)


@pytest.mark.parametrize("name", backup.FILES)
def test_corrupt_bundle_is_refused_before_copy(bundle, tmp_path, name):
    with (bundle / name).open("ab") as output:
        output.write(b"changed")
    with pytest.raises((backup.LocalBackupError, ValueError)):
        backup.copy_bundle(bundle, tmp_path / "target")
    assert not (tmp_path / "target").exists()
    assert not list(tmp_path.glob(backup.STAGING_PREFIX + "*"))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", True),
        ("secret_bearing", 1),
        ("postgresql_major", True),
        ("postgresql_major", 13),
        ("snapshot_method", "sqlite-online-backup-v1"),
        ("unknown", "ignored"),
        ("schema_revision", "obsolete"),
    ],
)
def test_manifest_versions_and_types_fail_closed(bundle, field, value):
    payload = json.loads((bundle / backup.MANIFEST_NAME).read_bytes())
    payload[field] = value
    write_manifest(bundle, payload)
    with pytest.raises(backup.LocalBackupError):
        backup.validate_bundle(bundle)


def test_rechecks_key_pair_even_with_recomputed_checksums(bundle):
    key = base64.urlsafe_b64encode(b"b" * 32) + b"\n"
    (bundle / backup.KEY_NAME).write_bytes(key)
    payload = json.loads((bundle / backup.MANIFEST_NAME).read_bytes())
    payload["key_sha256"] = hashlib.sha256(key).hexdigest()
    write_manifest(bundle, payload)
    with pytest.raises(backup.LocalBackupError, match="key_mismatch"):
        backup.validate_bundle(bundle)


@pytest.mark.parametrize("change", ["file_mode", "directory_mode", "extra", "symlink", "hardlink"])
def test_unsafe_bundle_paths_and_permissions_refused(bundle, tmp_path, change):
    if change == "file_mode":
        (bundle / backup.KEY_NAME).chmod(0o644)
    elif change == "directory_mode":
        bundle.chmod(0o755)
    elif change == "extra":
        (bundle / "extra").touch()
    elif change == "symlink":
        key = bundle / backup.KEY_NAME
        key.rename(tmp_path / "key")
        key.symlink_to(tmp_path / "key")
    else:
        os.link(bundle / backup.KEY_NAME, tmp_path / "linked")
    with pytest.raises((backup.LocalBackupError, OSError)):
        backup.validate_bundle(bundle)


def test_tar_roundtrip_is_fixed_inventory_private_and_does_not_extract_links(bundle, tmp_path):
    archive = tmp_path / "transfer.tar"
    with archive.open("xb") as output:
        backup.pack_bundle(bundle, output)
    backup.unpack_bundle(archive, tmp_path / "received")
    assert backup.validate_bundle(tmp_path / "received") == backup.validate_bundle(bundle)


@pytest.mark.parametrize("name", ["../../outside", "/absolute", "secrets/digest.key"])
def test_archive_traversal_and_links_refused_before_any_destination(tmp_path, name):
    archive = tmp_path / "malicious.tar"
    with tarfile.open(archive, "w") as output:
        for item_name in (*backup.FILES, "secrets"):
            item = tarfile.TarInfo(item_name if item_name != backup.KEY_NAME else name)
            item.mode = 0o700 if item_name == "secrets" else 0o600
            item.type = tarfile.DIRTYPE if item_name == "secrets" else tarfile.REGTYPE
            if item_name == backup.KEY_NAME:
                item.type = tarfile.SYMTYPE
                item.linkname = "../../outside"
            output.addfile(item)
    with pytest.raises(backup.LocalBackupError, match="archive_invalid"):
        backup.unpack_bundle(archive, tmp_path / "received")
    assert not (tmp_path / "received").exists()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///tmp/legacy.db",
        "postgresql+asyncpg://marketing_agents@localhost:5432/marketing_agents",
        URL.replace("localhost", "remote.example"),
        URL.replace("5432", "5433"),
        URL.replace("marketing_agents@", "postgres@"),
        URL.replace("marketing_agents@", "marketing_agents:secret@"),
        URL + "&sslmode=disable",
        URL + "&passfile=/tmp/password",
        URL.replace("host=/var", "host=relative/var"),
        URL.replace("host=/var", "host=/tmp,/var"),
    ],
)
def test_only_exact_unix_socket_application_identity_allowed(url):
    with pytest.raises((backup.LocalBackupError, ValueError)):
        backup.connection_options(url)


def test_pg_environment_discards_password_services_and_remote_overrides(monkeypatch):
    for name in ("PGPASSWORD", "PGSERVICE", "PGOPTIONS", "PGHOSTADDR", "DATABASE_URL"):
        monkeypatch.setenv(name, "secret-or-remote")
    environment = backup.tool_environment(URL)
    assert not set(environment) & {
        "PGPASSWORD",
        "PGSERVICE",
        "PGOPTIONS",
        "PGHOSTADDR",
        "DATABASE_URL",
    }
    assert environment["PGPASSFILE"] == "/nonexistent/marketing-agents-no-passfile"
    assert environment["PGSERVICEFILE"] == "/dev/null"
    assert environment["PGHOST"] == str(backup.SOCKET_DIRECTORY)
    assert backup.connection_options(URL)["ssl"] is False


def test_tool_is_bounded_has_no_shell_or_password_prompt_and_redacts_failure(monkeypatch):
    calls = []

    def failed(arguments, **kwargs):
        private_passfile = Path(kwargs["env"]["PGPASSFILE"])
        assert private_passfile.stat().st_mode & 0o777 == 0o600
        assert private_passfile.read_bytes() == b""
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=1, stdout=b"private", stderr=b"password=private")

    monkeypatch.setattr(backup.subprocess, "run", failed)
    with pytest.raises(backup.LocalBackupError, match=r"^postgresql_backup_tool_failed$"):
        backup.run_tool(["pg_dump", "--no-password"], URL)
    assert calls[0][1]["timeout"] == backup.TOOL_TIMEOUT
    assert "shell" not in calls[0][1]
    assert calls[0][1]["stdin"] == subprocess.DEVNULL


class FakeConnection:
    def __init__(self, key):
        self.key, self.active, self.closed, self.occupied = key, False, False, False
        self.queries = []
        self.major = 17

    @asynccontextmanager
    async def transaction(self, **options):
        assert options == {"isolation": "repeatable_read", "readonly": True}
        self.active = True
        try:
            yield
        finally:
            self.active = False

    async def fetchrow(self, sql):
        self.queries.append(sql)
        return {
            "role": "marketing_agents",
            "database": "marketing_agents",
            "version": self.major * 10000,
            "rolsuper": False,
            "rolreplication": False,
            "rolbypassrls": False,
            "rolcreatedb": False,
            "rolcreaterole": False,
        }

    async def fetchval(self, sql):
        self.queries.append(sql)
        if "pg_export_snapshot" in sql:
            assert self.active
            return "00000001-00000002-1"
        return self.occupied

    async def fetch(self, sql):
        self.queries.append(sql)
        if "alembic_version" in sql:
            return [{"version_num": backup.HEAD_REVISION}]
        if "local_runtime_identity" in sql:
            return [
                {
                    "singleton_id": 1,
                    "format_version": 1,
                    "key_fingerprint": manifest_payload(self.key)["key_fingerprint"],
                }
            ]
        return [
            {
                "content_version": "v1",
                "content_hash": manifest_payload(self.key)["catalog_hash"],
                "snapshot_json": json.dumps({"manifest": {"content_version": "v1"}}),
            }
        ]

    async def close(self):
        self.closed = True


@pytest.fixture
def fake_database(monkeypatch, bundle):
    connection = FakeConnection((bundle / backup.KEY_NAME).read_bytes())
    verified = []

    async def connect(**options):
        assert options == backup.connection_options(URL)
        return connection

    async def verify(url, key):
        verified.append((url, key))

    monkeypatch.setattr(backup.asyncpg, "connect", connect)
    monkeypatch.setattr(backup, "verify_local_installation", verify)
    monkeypatch.setattr(backup, "client_major", lambda *args: 17)
    return connection, verified


async def test_dump_and_metadata_use_same_live_exported_transaction(
    fake_database, bundle, tmp_path, monkeypatch
):
    connection, verified = fake_database

    def dump(arguments, url, *, output):
        assert connection.active
        assert arguments == [
            "pg_dump",
            "--no-password",
            "--format=custom",
            "--no-acl",
            "--snapshot=00000001-00000002-1",
        ]
        assert url == URL
        output.write(b"PGDMPconsistent")
        return b""

    monkeypatch.setattr(backup, "run_tool", dump)
    result = await backup.backup_postgresql(URL, bundle / backup.KEY_NAME, tmp_path / "new")
    assert backup.validate_bundle(tmp_path / "new") == result
    assert result.database_sha256 == hashlib.sha256(b"PGDMPconsistent").hexdigest()
    assert verified == [(URL, bundle / backup.KEY_NAME)]
    assert connection.closed and not connection.active


async def test_dump_failure_never_publishes_and_closes_snapshot(
    fake_database, bundle, tmp_path, monkeypatch
):
    connection, _ = fake_database

    def failed(*args, **kwargs):
        raise backup.LocalBackupError("tool_failed")

    monkeypatch.setattr(backup, "run_tool", failed)
    with pytest.raises(backup.LocalBackupError):
        await backup.backup_postgresql(URL, bundle / backup.KEY_NAME, tmp_path / "new")
    assert connection.closed
    assert not (tmp_path / "new").exists()
    assert not list(tmp_path.glob(backup.STAGING_PREFIX + "*"))


async def test_restore_is_single_transaction_no_clean_and_verifies_before_key_publish(
    fake_database, bundle, tmp_path, monkeypatch
):
    _, verified = fake_database
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    calls = []

    def restore(arguments, url):
        assert not list(secrets.iterdir())
        calls.append(arguments)

    monkeypatch.setattr(backup, "run_tool", restore)
    await backup.restore_into_empty_database(bundle, URL, secrets / "digest.key")
    assert calls[0][:7] == [
        "pg_restore",
        "--no-password",
        "--single-transaction",
        "--exit-on-error",
        "--no-owner",
        "--no-acl",
        "--dbname=marketing_agents",
    ]
    assert "--clean" not in calls[0] and "--create" not in calls[0]
    assert len(verified) == 2 and verified[-1] == (URL, secrets / "digest.key")
    assert (secrets / "digest.key").read_bytes() == (bundle / backup.KEY_NAME).read_bytes()


@pytest.mark.parametrize("reason", ["occupied", "wrong_major", "existing_key"])
async def test_restore_refuses_target_before_tool_or_key_mutation(
    fake_database, bundle, tmp_path, monkeypatch, reason
):
    connection, _ = fake_database
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    if reason == "occupied":
        connection.occupied = True
    elif reason == "wrong_major":
        connection.major = 16
    else:
        backup._write_private(secrets / "digest.key", b"existing")

    def forbidden(*args, **kwargs):
        pytest.fail("restore tool must not run against refused target")

    monkeypatch.setattr(backup, "run_tool", forbidden)
    with pytest.raises(backup.LocalBackupError):
        await backup.restore_into_empty_database(bundle, URL, secrets / "digest.key")
    if reason == "occupied":
        assert any("pg_type" in query and "pg_extension" in query for query in connection.queries)
    assert list(secrets.iterdir()) == ([] if reason != "existing_key" else [secrets / "digest.key"])


def test_helpers_are_network_none_fixed_socket_and_never_mount_database_data(monkeypatch):
    transport = backup.DockerTransport(PROJECT)
    calls = []
    monkeypatch.setattr(transport, "owned_volume", lambda *args, **kwargs: None)
    monkeypatch.setattr(transport, "remove_container", lambda name: None)
    monkeypatch.setattr(
        transport, "command", lambda args, **kwargs: calls.append((args, kwargs)) or b""
    )
    transport.helper(IMAGE, "_export", archive=io.BytesIO())
    arguments = calls[0][0]
    assert arguments[arguments.index("--network") + 1] == "none"
    assert arguments[arguments.index("--pull") + 1] == "never"
    assert arguments[arguments.index("--user") + 1] == "10001:10001"
    assert (
        "--read-only" in arguments and "ALL" in arguments and "no-new-privileges:true" in arguments
    )
    mounts = [arguments[i + 1] for i, value in enumerate(arguments) if value == "--mount"]
    assert mounts == [
        f"type=volume,source={PROJECT}_postgres-socket,target={backup.SOCKET_DIRECTORY},readonly",
        f"type=volume,source={PROJECT}_postgres-secrets,target={backup.SECRET_DIRECTORY},readonly",
    ]
    assert not any("_data," in value or "_local-secrets" in value for value in arguments)


def test_ownership_rejects_foreign_volume_before_cleanup(monkeypatch):
    transport = backup.DockerTransport(PROJECT)
    name = PROJECT + "_postgres-data"
    calls = []

    def command(arguments, **kwargs):
        calls.append(arguments)
        return json.dumps(
            [{"Name": name, "Labels": {"com.docker.compose.project": "foreign"}}]
        ).encode()

    monkeypatch.setattr(transport, "command", command)
    transport.created_volumes.append(name)
    with pytest.raises(backup.LocalBackupError, match="cleanup_failed"):
        transport.cleanup_created_volumes()
    assert calls[-1] == ["volume", "inspect", name]
    assert not any(args[:2] == ["volume", "rm"] for args in calls)


def test_legacy_volume_names_are_never_owned_or_removed():
    transport = backup.DockerTransport(PROJECT)
    for suffix in ("data", "local-secrets", "api-socket"):
        with pytest.raises(backup.LocalBackupError, match="volume_invalid"):
            transport.owned_volume(PROJECT + "_" + suffix)


def test_restore_cli_never_accepts_arbitrary_database_url(bundle):
    with pytest.raises(SystemExit):
        backup.main(["restore", "--mode", "native", "--backup", str(bundle), "--database-url", URL])


def test_content_free_cli_error_does_not_print_secret_path_or_database(monkeypatch, capsys):
    def failure(*args):
        raise RuntimeError("private-secret-value at /private/path user:password@server")

    monkeypatch.setattr(backup, "compose_backup", failure)
    assert backup.main(["backup", "--destination", "/private/path"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "code": "postgresql_backup_failed"}


def test_live_gate_requires_explicit_acquisition_and_does_not_accept_mixed_modes():
    for image, postgres, build in [(None, None, False), (IMAGE, None, False), (IMAGE, IMAGE, True)]:
        with pytest.raises(backup.LocalBackupError):
            BackupVerification(image, postgres, build=build)
    assert BackupVerification(None, None, build=True).build
    assert not BackupVerification(IMAGE, IMAGE, build=False).build


@pytest.mark.parametrize(
    "warning", [b"WARNING: incomplete dump", b"NOTICE: unexpected configuration"]
)
def test_success_exit_with_any_client_warning_is_still_refused(monkeypatch, warning):
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"", stderr=warning),
    )
    with pytest.raises(backup.LocalBackupError, match="tool_warning"):
        backup.run_tool(["pg_dump", "--no-password"], URL)


def test_native_backup_refuses_uninitialized_state_without_creating_database(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    with pytest.raises(backup.LocalBackupError, match="existing_native_state_required"):
        backup.native_backup(state, tmp_path / "target")
    assert not list(state.iterdir())
    assert not (tmp_path / "target").exists()


def test_native_restore_refuses_existing_target_before_starting_server(bundle, tmp_path):
    target = tmp_path / "owned-existing"
    target.mkdir(mode=0o700)
    with pytest.raises(backup.LocalBackupError, match="destination_exists"):
        backup.native_restore(bundle, target)
    assert not list(target.iterdir())


def test_native_backup_cli_selects_locked_state_route_and_rejects_mixed_routes(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(backup, "native_backup", lambda *args: calls.append(args))
    arguments = [
        "backup",
        "--mode",
        "native",
        "--state-dir",
        "/private/state",
        "--destination",
        "/private/bundle",
    ]
    assert backup.main(arguments) == 0
    assert calls == [(Path("/private/state"), Path("/private/bundle"))]
    assert backup.main([*arguments, "--database-url", URL]) == 1
    assert len(calls) == 1
    capsys.readouterr()


def test_docker_failure_propagates_only_bounded_guest_code(monkeypatch):
    transport = backup.DockerTransport(PROJECT)
    for stderr, expected in (
        (b'\x7b"code":"postgresql_backup_key_mismatch"}', "postgresql_backup_key_mismatch"),
        (b"private:password@remote", "postgresql_backup_docker_command_failed"),
        (
            b'\x7b"code":"postgresql_backup_password=private"}',
            "postgresql_backup_docker_command_failed",
        ),
    ):
        monkeypatch.setattr(
            backup.subprocess,
            "run",
            lambda *args, diagnostic=stderr, **kwargs: SimpleNamespace(
                returncode=1, stderr=diagnostic, stdout=b"private"
            ),
        )
        with pytest.raises(backup.LocalBackupError) as error:
            transport.command(["run", "example"])
        assert error.value.code == expected


def test_postgresql_server_is_owned_nonroot_without_network_or_key_mount(monkeypatch):
    transport = backup.DockerTransport(PROJECT)
    calls = []
    monkeypatch.setattr(transport, "owned_volume", lambda *args, **kwargs: None)
    monkeypatch.setattr(transport, "command", lambda args, **kwargs: calls.append(args) or b"id")
    monkeypatch.setattr(
        backup.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b"1\n", stderr=b""),
    )
    transport.start_server(IMAGE)
    args = calls[0]
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--user") + 1] == "999:10001"
    assert args[args.index("--pull") + 1] == "never"
    assert "--publish" not in args and "--privileged" not in args
    assert args[args.index("--cap-drop") + 1] == "ALL"
    mounts = [args[i + 1] for i, value in enumerate(args) if value == "--mount"]
    assert mounts == [
        f"type=volume,source={PROJECT}_postgres-data,target={backup.DATA_DIRECTORY}",
        f"type=volume,source={PROJECT}_postgres-socket,target={backup.SOCKET_DIRECTORY}",
    ]


def test_in_use_owned_volume_is_retained_and_successful_cleanup_is_idempotent(monkeypatch):
    transport = backup.DockerTransport(PROJECT)
    name = PROJECT + "_postgres-data"
    transport.created_volumes.append(name)
    monkeypatch.setattr(transport, "owned_volume", lambda *args, **kwargs: None)
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["volume", "ls"]:
            return name.encode()
        return b"live-container" if args[0] == "ps" else b""

    monkeypatch.setattr(transport, "command", command)
    with pytest.raises(backup.LocalBackupError, match="cleanup_failed"):
        transport.cleanup_created_volumes()
    assert transport.created_volumes == [name]
    assert not any(args[:2] == ["volume", "rm"] for args in calls)
    monkeypatch.setattr(transport, "command", lambda args, **kwargs: calls.append(args) or b"")
    transport.cleanup_created_volumes()
    before = list(calls)
    transport.cleanup_created_volumes()
    assert calls == before and not transport.created_volumes


def test_native_termination_unwinds_and_restores_original_handlers():
    numbers = (signal.SIGINT, signal.SIGTERM)
    original = {number: signal.getsignal(number) for number in numbers}
    with (
        pytest.raises(backup.LocalBackupError, match="interrupted"),
        backup.owned_signal_cleanup(),
    ):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
    assert {number: signal.getsignal(number) for number in numbers} == original


def test_unknown_create_outcome_is_tracked_before_mutation_and_recoverably_cleaned(monkeypatch):
    transport = backup.DockerTransport(PROJECT)
    calls = []
    monkeypatch.setattr(transport, "require_absent_project", lambda: None)
    monkeypatch.setattr(transport, "owned_volume", lambda *args, **kwargs: None)
    name = PROJECT + "_postgres-data"

    def command(args, **kwargs):
        calls.append(args)
        if args[:2] == ["volume", "create"]:
            assert transport.created_volumes == [name]
            raise subprocess.TimeoutExpired("docker", 180)
        if args[:2] == ["volume", "ls"]:
            return name.encode()
        return b""

    monkeypatch.setattr(transport, "command", command)
    with pytest.raises(subprocess.TimeoutExpired):
        transport.create_volumes()
    transport.cleanup_created_volumes()
    assert ["volume", "rm", name] in calls
    assert not transport.created_volumes


def test_verifier_continues_independent_cleanup_after_one_owned_failure():
    verification = BackupVerification(IMAGE, IMAGE, build=False)
    calls = []

    def fails():
        calls.append("failed-stop")
        raise backup.LocalBackupError("stop_failed")

    first = SimpleNamespace(
        stop_server=lambda: calls.append("first-stop"),
        cleanup_created_volumes=lambda: calls.append("first-volumes"),
    )
    second = SimpleNamespace(
        stop_server=fails, cleanup_created_volumes=lambda: calls.append("second-volumes")
    )
    verification.transports = [first, second]
    with pytest.raises(backup.LocalBackupError, match="cleanup_failed"):
        verification.cleanup()
    assert calls == ["failed-stop", "second-volumes", "first-stop", "first-volumes"]
    assert "cleanup" not in verification.report


def test_public_compose_backup_sigterm_unwinds_real_helper_cleanup(monkeypatch, tmp_path):
    transport = backup.DockerTransport(PROJECT)
    removed = []
    monkeypatch.setattr(backup, "DockerTransport", lambda project: transport)
    monkeypatch.setattr(transport, "local_daemon", lambda: None)
    monkeypatch.setattr(transport, "source_image", lambda: IMAGE)
    monkeypatch.setattr(transport, "owned_volume", lambda *args, **kwargs: None)
    monkeypatch.setattr(transport, "remove_container", lambda name: removed.append(name))
    original = signal.getsignal(signal.SIGTERM)

    def interrupted(*args, **kwargs):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(transport, "command", interrupted)
    with pytest.raises(backup.LocalBackupError, match="interrupted"):
        backup.compose_backup(PROJECT, tmp_path / "target")
    assert len(removed) == 1 and removed[0].startswith("marketing-agents-pg-transfer-")
    assert signal.getsignal(signal.SIGTERM) == original
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize("stop_fails", [False, True])
def test_public_restore_interruption_attempts_server_and_unused_volume_cleanup(
    monkeypatch, bundle, stop_fails
):
    calls = []

    def stop():
        calls.append("stop")
        if stop_fails:
            raise backup.LocalBackupError("stop_failed")

    def interrupted(*args, **kwargs):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    transport = SimpleNamespace(
        local_daemon=lambda: None,
        require_absent_project=lambda: None,
        image_id=lambda image: IMAGE,
        create_volumes=lambda: calls.append("volumes"),
        start_server=lambda image: calls.append("start"),
        helper=interrupted,
        stop_server=stop,
        cleanup_created_volumes=lambda: calls.append("cleanup"),
    )
    monkeypatch.setattr(backup, "DockerTransport", lambda project: transport)
    original = signal.getsignal(signal.SIGTERM)
    with pytest.raises(
        backup.LocalBackupError, match="cleanup_failed" if stop_fails else "interrupted"
    ):
        backup.compose_restore(PROJECT, IMAGE, IMAGE, bundle)
    assert calls == ["volumes", "start", "stop", "cleanup"]
    assert signal.getsignal(signal.SIGTERM) == original


def test_cleanup_shields_termination_but_never_masks_deadline():
    original = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGALRM)}
    with backup.cleanup_signal_shield():
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
        assert signal.getsignal(signal.SIGALRM) == original[signal.SIGALRM]
    assert {number: signal.getsignal(number) for number in original} == original


def test_verifier_cleanup_never_swallows_deadline_as_an_ordinary_resource_failure():
    verification = BackupVerification(IMAGE, IMAGE, build=False)
    calls = []

    def expired():
        calls.append("deadline")
        raise VerificationInterrupted()

    verification.transports = [
        SimpleNamespace(
            stop_server=expired, cleanup_created_volumes=lambda: calls.append("past-deadline")
        )
    ]
    with pytest.raises(VerificationInterrupted):
        verification.cleanup()
    assert calls == ["deadline"]


def test_execution_expiry_bounds_nested_cleanup_without_renewing_outer_budget(monkeypatch):
    now = [0.0]
    timers = []
    monkeypatch.setattr(verifier.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(verifier.signal, "setitimer", lambda timer, seconds: timers.append(seconds))
    deadline = VerificationDeadline()
    deadline.start()
    verification = BackupVerification(IMAGE, IMAGE, build=False)
    verification.deadline = deadline
    transport = verification.tracked_transport_class()(PROJECT)
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(backup.subprocess, "run", run)
    now[0] = 1200.0
    with pytest.raises(VerificationInterrupted):
        try:
            deadline.expired(signal.SIGALRM, None)
        finally:
            # A nested helper finally must already share the 150-second budget.
            assert deadline.cleanup_until == 1350.0
            now[0] = 1300.0
            transport.command(["ps"])
            assert calls == [50.0]  # active subprocess, not only its entry, is bounded
    now[0] = 1340.0
    deadline.begin_cleanup()  # main's finally must not restart another 150 seconds
    assert deadline.cleanup_until == 1350.0 and timers == [1200, 150]
    transport.command(["ps"])
    assert calls == [50.0, 10.0]
    now[0] = 1350.0
    with pytest.raises(VerificationInterrupted):
        try:
            deadline.expired(signal.SIGALRM, None)
        finally:
            # Even further finally blocks cannot launch a new cleanup command.
            transport.command(["ps"])
    with pytest.raises(VerificationInterrupted):
        deadline.begin_cleanup()
    assert calls == [50.0, 10.0] and timers == [1200, 150]
