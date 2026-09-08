"""DEL-05 private API Unix transport preserves local HTTP controls and ownership."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import stat
import sys
import tempfile
from pathlib import Path

import pytest
from httpx import AsyncClient, AsyncHTTPTransport
from marketing_agents.workers.runtime.unix_listener import unix_listener, validate_socket_path

from tests.integration.runtime.test_del_05_process_composition import _installation


@pytest.fixture
def socket_path():
    # macOS Unix names are bounded to 104 bytes; pytest's nested names exceed it.
    directory = Path(tempfile.mkdtemp(prefix="del05-uds-", dir=Path("/tmp").resolve()))
    os.chown(directory, -1, os.getgid())
    directory.chmod(0o750)
    path = directory / "api.sock"
    yield path
    if path.exists() or path.is_symlink():
        path.unlink()
    directory.rmdir()


def test_del_05_unix_socket_permissions_and_owned_stale_recovery(socket_path: Path) -> None:
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    with unix_listener(str(socket_path)) as listener:
        assert listener.fileno() >= 0
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o660
        assert socket_path.stat().st_uid == os.getuid()
        with pytest.raises(ValueError, match="active listener"), unix_listener(str(socket_path)):
            pytest.fail("a second process replaced the live listener")
    assert not socket_path.exists()


def test_del_05_unix_socket_refuses_other_files_and_unsafe_directory(socket_path: Path) -> None:
    socket_path.write_text("preserve this file", encoding="utf-8")
    with pytest.raises(ValueError, match="non-socket"), unix_listener(str(socket_path)):
        pytest.fail("a regular file was replaced")
    assert socket_path.read_text(encoding="utf-8") == "preserve this file"
    socket_path.parent.chmod(0o777)
    try:
        with pytest.raises(ValueError, match="0750"):
            validate_socket_path(str(socket_path))
        assert socket_path.parent.stat().st_mode & 0o777 == 0o777
    finally:
        socket_path.parent.chmod(0o750)
    with pytest.raises(ValueError, match="absolute"):
        validate_socket_path("relative.sock")


@pytest.mark.asyncio
async def test_del_05_real_unix_api_session_health_and_shutdown(
    tmp_path: Path, socket_path: Path
) -> None:
    settings = await _installation(tmp_path)
    environment = {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "CATALOG_ROOT": str(settings.catalog_root),
        "MARKETING_AGENTS_DIGEST_KEY_PATH": str(settings.marketing_agents_digest_key_path),
        "MARKETING_AGENTS_API_SOCKET": str(socket_path),
        "APP_ENV": "test",
        "AUTH_MODE": "local",
        "LLM_PROVIDER": "mock",
        "CONNECTOR_MODE": "mock",
        "ALLOW_EXTERNAL_NETWORK": "false",
        "API_HOST": "127.0.0.1",
        "API_PORT": "18001",
        "API_TRUSTED_ORIGINS": '["http://127.0.0.1:18001"]',
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "marketing_agents.workers.serve_api",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
        cwd=tmp_path,
    )
    try:
        async with AsyncClient(
            transport=AsyncHTTPTransport(uds=str(socket_path)),
            base_url="http://127.0.0.1:18001",
            timeout=10,
        ) as client:
            for _ in range(100):
                if process.returncode is not None:
                    _, error = await process.communicate()
                    pytest.fail(error.decode())
                if socket_path.exists():
                    response = await client.get("/health/live")
                    if response.status_code == 200:
                        break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Unix API did not become live")
            assert socket_path.stat().st_mode & 0o777 == 0o660
            session = await client.get("/api/v1/session")
            assert session.status_code == 200, session.text
            assert session.json()["modelMode"] == "mock"
            assert session.json()["networkPermission"] is False
            denied = await client.post(
                "/api/v1/demo-scenarios/demo.social-media.content-draft.v1/runs",
                json={},
                headers={"Origin": "https://untrusted.example"},
            )
            assert denied.status_code == 403
        process.send_signal(signal.SIGTERM)
        _, error = await asyncio.wait_for(process.communicate(), timeout=5)
        assert process.returncode == 0, error.decode()
        assert not socket_path.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
