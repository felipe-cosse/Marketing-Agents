"""AC-15: collection/session and per-test external-network denial and accounting."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.network.python_network_guard import (
    InstalledNetworkGuard,
    install_network_guard,
    network_attempt_counts,
    network_guard_installed,
)

# Root conftest import precedes test-module collection and child conftests.
# Arbitrary plugins loaded before this point and ordinary subprocesses are not
# monkeypatch-sandboxed; the AC-15 kernel runner owns those boundaries.
_session_baseline = network_attempt_counts()[1]
_session_guard = install_network_guard()
_attempt_baseline = pytest.StashKey[int]()
_fixture_guards = pytest.StashKey[list[InstalledNetworkGuard]]()
_finalizations = pytest.StashKey[int]()
_WORKER_PROOF = "ac15_unacknowledged_network_denials_v1"
_FINAL_PROOF_INPUT = "ac15_final_network_proof_v1"
_MAX_WORKER_COUNT = 2**31 - 1
_MAX_PROOF_BYTES = 256


@dataclass
class _WorkerProofs:
    directory: tempfile.TemporaryDirectory
    paths: dict[object, tuple[Path, str]] = field(default_factory=dict)
    reported: dict[object, int] = field(default_factory=dict)
    invalid: bool = False


_worker_proofs = pytest.StashKey[_WorkerProofs]()


def _bounded_count(value):
    return type(value) is int and 0 <= value <= _MAX_WORKER_COUNT


def _unique_proof_fields(pairs):
    proof = {}
    for name, value in pairs:
        if name in proof:
            raise ValueError("duplicate_worker_proof_field")
        proof[name] = value
    return proof


def _publish_final_worker_proof(config, count, complete):
    # This descriptor is created only by our controller's configure-node hook.
    # There is no caller option or environment variable accepting a proof path.
    descriptor = config.workerinput.get(_FINAL_PROOF_INPUT)
    if (
        type(descriptor) is not dict
        or set(descriptor) != {"path", "token"}
        or type(descriptor["path"]) is not str
        or type(descriptor["token"]) is not str
    ):
        return  # Missing final proof fails on the controller.
    path = Path(descriptor["path"])
    if not path.is_absolute() or path.name != descriptor["token"] + ".json":
        return
    calls = config.stash.get(_finalizations, 0) + 1
    config.stash[_finalizations] = calls
    proof = {
        "version": 1,
        "token": descriptor["token"],
        "count": min(count, _MAX_WORKER_COUNT),
        "finalizations": min(calls, 2),
        "complete": complete,
    }
    encoded = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_PROOF_BYTES:
        return
    temporary = path.with_suffix(".pending")
    try:
        descriptor_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor_fd, "wb") as stream:
            stream.write(encoded)
        os.replace(temporary, path)
    except OSError:
        # A partial or missing proof cannot pass; never expose local paths.
        return


def _collect_final_worker_proofs(config):
    state = config.stash.get(_worker_proofs, None)
    if state is None:
        return 0, False
    count = 0
    invalid = state.invalid or set(state.paths) != set(state.reported)
    for node, (path, token) in state.paths.items():
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("invalid_worker_proof_type")
                encoded = stream.read(_MAX_PROOF_BYTES + 1)
            proof = (
                json.loads(encoded, object_pairs_hook=_unique_proof_fields)
                if len(encoded) <= _MAX_PROOF_BYTES
                else None
            )
        except (OSError, UnicodeDecodeError, ValueError):
            proof = None
        if (
            type(proof) is not dict
            or set(proof) != {"version", "token", "count", "finalizations", "complete"}
            or type(proof["version"]) is not int
            or proof["version"] != 1
            or proof["token"] != token
            or not _bounded_count(proof["count"])
            or type(proof["finalizations"]) is not int
            or proof["finalizations"] != 1
            or proof["complete"] is not True
            or proof["count"] < state.reported.get(node, 0)
        ):
            invalid = True
        else:
            count = min(_MAX_WORKER_COUNT, count + proof["count"])
    return count, invalid


def pytest_configure(config):
    def before(hook_name, hook_impls, kwargs):
        pass

    def after(outcome, hook_name, hook_impls, kwargs):
        if hook_name != "pytest_sessionfinish":
            return
        session = kwargs["session"]
        count = network_attempt_counts()[1] - _session_baseline
        if hasattr(config, "workerinput"):
            _publish_final_worker_proof(config, count, outcome.excinfo is None)
            invalid = False
        else:
            workers, invalid = _collect_final_worker_proofs(config)
            count += workers
        if count or invalid:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            reporter = config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                if count:
                    reporter.write_sep("=", f"AC-15: {count} unacknowledged network denial(s)")
                if invalid:
                    reporter.write_sep("=", "AC-15: missing or invalid worker network-denial proof")

    # Public monitoring runs after every hook wrapper has unwound. On the
    # controller this also follows xdist's bounded worker teardown, avoiding a
    # race with xdist's earlier workerfinished event and arbitrary hook ordering.
    undo = config.pluginmanager.add_hookcall_monitoring(before, after)
    config.add_cleanup(undo)


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node):
    # These proofs use the same private local filesystem as the fixed popen
    # workers. Remote workers cannot establish this evidence contract.
    spec = node.gateway.spec
    if not spec.popen or any(getattr(spec, name, None) for name in ("ssh", "socket", "via")):
        raise pytest.UsageError("AC-15: network evidence requires local popen workers")
    state = node.config.stash.get(_worker_proofs, None)
    if state is None:
        directory = tempfile.TemporaryDirectory(prefix="marketing-agents-ac15-workers-")
        state = _WorkerProofs(directory)
        node.config.stash[_worker_proofs] = state
        node.config.add_cleanup(directory.cleanup)
    if node in state.paths:
        state.invalid = True
        raise pytest.UsageError("AC-15: duplicate worker network-proof assignment")
    token = uuid.uuid4().hex
    path = Path(state.directory.name) / (token + ".json")
    state.paths[node] = path, token
    node.workerinput[_FINAL_PROOF_INPUT] = {"path": str(path), "token": token}


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    state = node.config.stash.get(_worker_proofs, None)
    if state is None:
        raise pytest.UsageError("AC-15: unassigned worker network proof")
    output = getattr(node, "workeroutput", None)
    count = output.get(_WORKER_PROOF) if isinstance(output, dict) else None
    if (
        error is not None
        or node not in state.paths
        or node in state.reported
        or not _bounded_count(count)
    ):
        state.invalid = True
        return
    state.reported[node] = count


def pytest_sessionfinish(session, exitstatus):
    # xdist emits workerfinished before all surrounding wrappers finish. This
    # early snapshot is validated, but only the later atomic proof is final.
    workeroutput = getattr(session.config, "workeroutput", None)
    if workeroutput is not None:
        workeroutput[_WORKER_PROOF] = min(
            network_attempt_counts()[1] - _session_baseline, _MAX_WORKER_COUNT
        )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    item.stash[_attempt_baseline] = network_attempt_counts()[1]
    item.config.stash[_fixture_guards] = []


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef, request):
    # Runs before the fixture body, including module/session autouse fixtures.
    # pytest-socket's setup hook can replace resolvers before any fixture runs.
    # Restore only after all fixture teardown, not immediately after a yield
    # fixture's setup (which would prematurely remove its active guard).
    if not network_guard_installed():
        request.config.stash[_fixture_guards].append(install_network_guard())


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    try:
        yield
    finally:
        for guard in reversed(item.config.stash.get(_fixture_guards, [])):
            guard.restore()
    baseline = item.stash.get(_attempt_baseline, network_attempt_counts()[1])
    count = network_attempt_counts()[1] - baseline
    if count:
        pytest.fail(
            f"AC-15: test attempted {count} forbidden network operation(s), even if caught",
            pytrace=False,
        )


def pytest_unconfigure(config):
    _session_guard.restore()


@pytest.fixture(autouse=True)
def _safe_11_no_external_network():
    # Ensure a fixture setup boundary even for a test with no explicit fixtures.
    # Installation is owned by pytest_fixture_setup so higher scopes are covered.
    yield
