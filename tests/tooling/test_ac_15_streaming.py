"""AC-15 streaming parity, exact raw-byte evidence and bounded record assembly."""

from __future__ import annotations

import hashlib
import io
import subprocess
from pathlib import Path

import pytest

from scripts import ac_15_offline as offline
from scripts.del_05_clean_state import VerificationFailure

CONNECTED = b'42 sendto(3, ""..., 1, 0, NULL, 0) = 1'
UNIX = b'43 connect(3, {sa_family=AF_UNIX, sun_path="private"}, 16) = 0'
EXTERNAL = (
    b'44 connect(3, {sa_family=AF_INET, sin_addr=inet_addr("192.0.2.1")}, 16) = -1 ENETUNREACH'
)
FILTER = b"45 seccomp(SECCOMP_SET_MODE_FILTER, 0, {len=1, filter=0x1234}) = 0"


def both(tmp_path, data):
    path = tmp_path / "network.trace"
    path.write_bytes(data)
    return offline.trace_summary(data), offline.trace_file_summary(path)


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_ac_15_stream_byte_file_parity_and_exact_hash_without_required_final_newline(
    tmp_path, ending
):
    data = CONNECTED + b"\r\n\r\n" + UNIX + ending
    byte, file = both(tmp_path, data)
    assert byte == file
    assert file["connected_send"] == file["unix"] == 1
    assert file["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\r", "\v", "\x85", "\u2028", "\u2029"])
def test_ac_15_stream_preserves_existing_splitlines_semantics(tmp_path, separator):
    data = separator.join([CONNECTED.decode(), UNIX.decode(), ""]).encode()
    byte, file = both(tmp_path, data)
    assert byte == file
    assert file["connected_send"] == file["unix"] == 1
    assert file["sha256"] == hashlib.sha256(data).hexdigest()


def test_ac_15_stream_empty_and_blank_bytes_are_all_hashed(tmp_path):
    summaries = []
    for data in (b"", b"\n", b"\r\n", b"\n\r\n\t  "):
        byte, file = both(tmp_path, data)
        assert byte == file
        assert file["sha256"] == hashlib.sha256(data).hexdigest()
        assert all(
            value == 0 for key, value in file.items() if key not in {"schema_version", "sha256"}
        )
        summaries.append(file["sha256"])
    assert len(set(summaries)) == 4


@pytest.mark.parametrize("late,category", [(EXTERNAL, "rejected"), (FILTER, "filter_changes")])
def test_ac_15_stream_never_omits_late_external_or_filter_records(tmp_path, late, category):
    data = (CONNECTED + b"\n") * 257 + late + b"\n"
    byte, file = both(tmp_path, data)
    assert byte == file
    assert file["connected_send"] == 257
    assert file[category] == file["rejected"] == 1
    assert file["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize(
    "late,error,code",
    [
        (b"invented(1) = 0", VerificationFailure, "unclassified_record"),
        (b"46 prctl(PR_UNKNOWN, 0) = 0", VerificationFailure, "prctl_operation_unparsed"),
        (b"47 connect(3, 0x1234, 16) = -1 EFAULT", VerificationFailure, "destination_unparsed"),
        (b"\xff", UnicodeDecodeError, ""),
        (b"48 +++ exited with 0 +++\xff", UnicodeDecodeError, ""),
        (b"\t\xff", UnicodeDecodeError, ""),
    ],
)
def test_ac_15_stream_rejects_late_unknown_or_invalid_utf8(tmp_path, late, error, code):
    data = (CONNECTED + b"\n") * 257 + late
    path = tmp_path / "network.trace"
    path.write_bytes(data)
    for parse in (lambda: offline.trace_summary(data), lambda: offline.trace_file_summary(path)):
        with pytest.raises(error, match=code or None):
            parse()


def test_ac_15_stream_total_bound_is_exact_for_both_apis(tmp_path, monkeypatch):
    data = CONNECTED + b"\n" + UNIX + b"\n"
    monkeypatch.setattr(offline, "MAX_TRACE_BYTES", len(data))
    assert both(tmp_path, data)[0] == both(tmp_path, data)[1]
    path = tmp_path / "network.trace"
    path.write_bytes(data + b"\n")
    for parse in (
        lambda: offline.trace_summary(data + b"\n"),
        lambda: offline.trace_file_summary(path),
    ):
        with pytest.raises(VerificationFailure, match="network_trace_exceeds_bound"):
            parse()


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_ac_15_stream_physical_bound_counts_raw_bytes_including_endings(
    tmp_path, monkeypatch, ending
):
    data = UNIX.replace(b"private", "caf\u00e9".encode()) + ending
    monkeypatch.setattr(offline, "MAX_TRACE_RECORD_BYTES", len(data))
    byte, file = both(tmp_path, data)
    assert byte == file and file["unix"] == 1
    monkeypatch.setattr(offline, "MAX_TRACE_RECORD_BYTES", len(data) - 1)
    path = tmp_path / "network.trace"
    for parse in (lambda: offline.trace_summary(data), lambda: offline.trace_file_summary(path)):
        with pytest.raises(VerificationFailure, match="network_trace_record_exceeds_bound"):
            parse()


def test_ac_15_stream_assembled_bound_is_in_bytes_before_concatenation(tmp_path, monkeypatch):
    prefix = 'sendto(3, "' + "\u00e9" * 40
    suffix = "z" * 40 + '", 120, 0, NULL, 0) = 120'
    data = ("1 " + prefix + " <unfinished ...>\n1 <... sendto resumed>" + suffix).encode()
    bound = len((prefix + suffix).encode())
    assert max(map(len, data.splitlines(keepends=True))) < bound
    monkeypatch.setattr(offline, "MAX_TRACE_RECORD_BYTES", bound)
    byte, file = both(tmp_path, data)
    assert byte == file and file["connected_send"] == 1
    monkeypatch.setattr(offline, "MAX_TRACE_RECORD_BYTES", bound - 1)
    path = tmp_path / "network.trace"
    for parse in (lambda: offline.trace_summary(data), lambda: offline.trace_file_summary(path)):
        with pytest.raises(VerificationFailure, match="assembled_record_exceeds_bound"):
            parse()


def test_ac_15_stream_repeated_resume_chain_stays_bounded(tmp_path, monkeypatch):
    fragments = ['sendto(3, "' + "x" * 40, "y" * 40, "z" * 40]
    data = (
        "1 " + fragments[0] + " <unfinished ...>\n"
        "1 <... sendto resumed>" + fragments[1] + " <unfinished ...>\n"
        "1 <... sendto resumed>" + fragments[2] + '", 120, 0, NULL, 0) = 120\n'
    ).encode()
    monkeypatch.setattr(offline, "MAX_TRACE_RECORD_BYTES", 128)
    assert max(map(len, data.splitlines(keepends=True))) <= 128
    path = tmp_path / "network.trace"
    path.write_bytes(data)
    for parse in (lambda: offline.trace_summary(data), lambda: offline.trace_file_summary(path)):
        with pytest.raises(VerificationFailure, match="assembled_record_exceeds_bound"):
            parse()


def test_ac_15_stream_pending_pid_bound_and_completed_slot_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "MAX_TRACE_PENDING_PIDS", 2)

    def start(pid):
        return f"{pid} connect(3, <unfinished ...>\n"

    def end(pid):
        return f'{pid} <... connect resumed>{{sa_family=AF_UNIX, sun_path="x"}}, 16) = 0\n'

    data = (start(1) + start(2) + end(2) + start(3) + end(1) + end(3)).encode()
    byte, file = both(tmp_path, data)
    assert byte == file and file["unix"] == 3
    overflow = (start(1) + start(2) + start(3)).encode()
    path = tmp_path / "network.trace"
    path.write_bytes(overflow)
    for parse in (
        lambda: offline.trace_summary(overflow),
        lambda: offline.trace_file_summary(path),
    ):
        with pytest.raises(VerificationFailure, match="pending_pids_exceed_bound"):
            parse()


@pytest.mark.parametrize(
    "data,code",
    [
        (b"1 connect(3, <unfinished ...>", "unfinished_call"),
        (b"1 <... connect resumed>) = 0", "orphan_resume"),
        (b"1 connect(3, <unfinished ...>\n1 <... sendto resumed>) = 0", "mismatched_resume"),
        (b"1 connect(3, <unfinished ...>\n1 connect(4, <unfinished ...>", "overlapping_call"),
        (b"1 connect(3, {sa_family=AF_UNIX})", "incomplete_call"),
    ],
)
def test_ac_15_stream_keeps_every_pending_pid_and_completion_failure(tmp_path, data, code):
    path = tmp_path / "network.trace"
    path.write_bytes(data)
    for parse in (lambda: offline.trace_summary(data), lambda: offline.trace_file_summary(path)):
        with pytest.raises(VerificationFailure, match=code):
            parse()


def test_ac_15_stream_file_never_uses_unbounded_read_or_text_api(tmp_path, monkeypatch):
    data = (CONNECTED + b"\n") * 4096 + UNIX
    calls = []

    class BoundedReader(io.BytesIO):
        def read(self, *args):
            pytest.fail("whole-file read is forbidden")

        def readline(self, size=-1):
            assert 0 < size <= offline.MAX_TRACE_RECORD_BYTES + 1
            calls.append(size)
            return super().readline(size)

    def forbidden(*args, **kwargs):
        pytest.fail("whole-file or text Path API is forbidden")

    source = BoundedReader(data)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: source)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    report = offline.trace_file_summary(tmp_path / "not-opened-on-host.trace")
    assert report["connected_send"] == 4096 and report["unix"] == 1
    assert report["sha256"] == hashlib.sha256(data).hexdigest()
    assert len(calls) == 4098 and source.closed


def test_ac_15_collect_trace_uses_file_api_and_preserves_child_diagnostics(tmp_path, monkeypatch):
    observed = []

    def run(command, **kwargs):
        path = Path(command[command.index("-o") + 1])
        path.write_bytes(CONNECTED + b"\n")
        return subprocess.CompletedProcess(command, 7, b"private output", b"")

    original = offline.trace_file_summary

    def file_summary(path):
        observed.append(path)
        return original(path)

    monkeypatch.setattr(offline.subprocess, "run", run)
    monkeypatch.setattr(offline, "trace_file_summary", file_summary)
    monkeypatch.setattr(
        offline, "trace_summary", lambda data: pytest.fail("byte API used for file")
    )
    result, report = offline.collect_trace(("python", "child.py"), timeout=1)
    assert result.returncode == 7 and result.stdout == b"private output"
    assert report["connected_send"] == 1
    assert observed and all(not path.exists() for path in observed)


def test_ac_15_stream_explicit_production_resource_limits():
    assert offline.MAX_TRACE_BYTES == 512 * 1024 * 1024
    assert offline.MAX_TRACE_RECORD_BYTES == 1024 * 1024
    assert offline.MAX_TRACE_PENDING_PIDS == 1024
    assert offline.SUITE_TIMEOUT == 1700
