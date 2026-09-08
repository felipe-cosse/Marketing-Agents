"""Owned, permission-bounded Unix transport for the container's local API."""

from __future__ import annotations

import errno
import os
import socket
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _same_socket(path: Path, device: int, inode: int) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return (
        metadata.st_dev == device
        and metadata.st_ino == inode
        and metadata.st_uid == os.getuid()
        and stat.S_ISSOCK(metadata.st_mode)
    )


def validate_socket_path(value: str) -> Path:
    path = Path(value)
    if (
        not path.is_absolute()
        or path.name in {"", ".", ".."}
        or ".." in path.parts
        or len(os.fsencode(path)) > 100
        or path.parent.is_symlink()
    ):
        raise ValueError("API socket requires one explicit absolute path")
    parent = path.parent.stat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or parent.st_gid != os.getgid()
        or stat.S_IMODE(parent.st_mode) != 0o750
    ):
        raise ValueError("API socket directory requires service ownership and mode 0750")
    return path


def _remove_owned_stale_socket(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if metadata.st_uid != os.getuid() or not stat.S_ISSOCK(metadata.st_mode):
        raise ValueError("API socket path is occupied by an unowned or non-socket file")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect(str(path))
        except OSError as error:
            if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise ValueError("API socket availability cannot be verified") from None
        else:
            raise ValueError("API socket already has an active listener")
    if not _same_socket(path, metadata.st_dev, metadata.st_ino):
        raise ValueError("API socket changed while checking its owner")
    path.unlink()


@contextmanager
def unix_listener(value: str) -> Iterator[socket.socket]:
    """Bind an exact private socket, never overwriting a live endpoint or file."""
    path = validate_socket_path(value)
    _remove_owned_stale_socket(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity: tuple[int, int] | None = None
    try:
        previous_umask = os.umask(0o117)
        try:
            listener.bind(str(path))
        finally:
            os.umask(previous_umask)
        metadata = path.lstat()
        identity = (metadata.st_dev, metadata.st_ino)
        os.chmod(path, 0o660)
        listener.listen(128)
        listener.setblocking(False)
        yield listener
    finally:
        listener.close()
        if identity is not None and _same_socket(path, *identity):
            path.unlink()
