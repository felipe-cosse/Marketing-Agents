"""Bounded clean-commit verification, with an exact Docker resource allowlist.

Build acquisition can contact registries. Backend containers and prebuilt tests
use --network none. Only the static web proxy joins the scoped ingress bridge;
it retains an outbound route, while fixed Unix-socket ingress isolates the API.
No caller source, .env, provider credentials, or developer volumes are mounted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

if __package__:
    from scripts.del_05_offline_diagnostics import project_diagnostics
else:
    from del_05_offline_diagnostics import project_diagnostics

REQUIRED = (
    "compose.yaml",
    "docker/api.Dockerfile",
    "docker/web.Dockerfile",
    "uv.lock",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "package.json",
    ".python-version",
    ".nvmrc",
    "scripts/del_05_runtime_smoke.py",
    "scripts/del_05_replay_fixture.py",
    "Makefile",
)
DATABASE = "/var/lib/marketing-agents/data/marketing_agents.db"
KEY = "/var/lib/marketing-agents/secrets/digest.key"
API_SOCKET = "/var/run/marketing-agents/api.sock"
VOLUMES = ("data", "local-secrets", "api-socket")
BACKENDS = ("api", "run-worker", "scheduler-worker", "local-secret-init", "migrate-seed")
RUNTIME_SERVICES = ("api", "web", "run-worker", "scheduler-worker")
PROJECT_PATTERN = re.compile(r"^marketing-agents-del05-[0-9a-f]{16}$")
PUBLIC_FIXTURE_HMAC = "del-05-public-test-signing-material-never-use-as-a-secret"
EXECUTION_SECONDS = 1620
CLEANUP_SECONDS = 120


class VerificationFailure(RuntimeError):
    pass


def require(condition: object, code: str) -> None:
    if not condition:
        raise VerificationFailure(code)


@contextmanager
def verification_signals(seconds: float) -> Iterator[None]:
    """Expire before the outer gate kills Make, retaining time for owned cleanup."""
    handled = (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
    previous = {signum: signal.getsignal(signum) for signum in handled}
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def interrupted(signum, _frame):
        raise VerificationFailure(
            "verification_execution_deadline"
            if signum == signal.SIGALRM
            else "verification_interrupted"
        )

    try:
        for signum in handled:
            signal.signal(signum, interrupted)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        remaining, interval = previous_timer
        if remaining > 0:
            # A pre-existing timer keeps its elapsed-time semantics; if it became
            # due while this verifier owned SIGALRM, deliver it promptly afterward.
            remaining = max(0.001, remaining - (time.monotonic() - started))
        signal.setitimer(signal.ITIMER_REAL, remaining, interval)


def clean_environment(source: dict[str, str]) -> dict[str, str]:
    # HOME/DOCKER_* are needed only by the host CLI for the selected engine and
    # registry acquisition. Compose passes exclusively its explicit environment.
    retained = {
        name: source[name]
        for name in (
            "PATH",
            "HOME",
            "TMPDIR",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        if name in source
    }
    return {
        **retained,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "COMPOSE_DISABLE_ENV_FILE": "1",
        # A saved remote buildx selection must not receive the tracked export.
        "BUILDX_BUILDER": "default",
    }


def validate_ref(ref: str) -> None:
    require(
        bool(ref)
        and not ref.startswith("-")
        and len(ref) <= 200
        and re.fullmatch(r"[A-Za-z0-9_./^~{}-]+", ref),
        "unsafe_source_ref",
    )


def validate_docker_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    require(
        parsed.scheme == "unix"
        and not parsed.netloc
        and parsed.path.startswith("/")
        and parsed.path != "/"
        and not parsed.query
        and not parsed.fragment,
        "verification_requires_local_unix_docker_engine",
    )
    return endpoint


def validate_docker_engine_version(version: object) -> None:
    require(
        isinstance(version, str)
        and re.fullmatch(r"[0-9]+(?:\.[0-9A-Za-z.-]+)*", version)
        and int(version.split(".", 1)[0]) >= 28,
        "verification_requires_docker_engine_28_or_newer",
    )


def export_archive(archive: Path, destination: Path) -> None:
    """Reject links and escaping members before extracting any tracked bytes."""
    require(destination.is_dir() and not any(destination.iterdir()), "export_must_be_empty")
    with tarfile.open(archive, "r:") as source:
        members = source.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            require(
                not name.is_absolute()
                and ".." not in name.parts
                and bool(name.parts)
                and (member.isfile() or member.isdir()),
                "unsafe_archive_member",
            )
            require(not name.is_absolute() and ".." not in name.parts, "unsafe_archive_path")
            require(
                all(
                    (part != ".env" and not part.startswith(".env.")) or part == ".env.example"
                    for part in name.parts
                ),
                "tracked_environment_file_forbidden",
            )
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = source.extractfile(member)
                require(stream is not None, "archive_file_missing")
                with target.open("xb") as output:
                    shutil.copyfileobj(stream, output)
                target.chmod(member.mode & 0o777)


def tree_fingerprint(root: Path) -> str:
    value = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            value.update(str(path.relative_to(root)).encode())
            value.update(b"\0")
            value.update(path.read_bytes())
    return value.hexdigest()


def validate_owned_resource(kind: str, name: str, project: str, inspected: dict) -> None:
    require(PROJECT_PATTERN.fullmatch(project), "unsafe_cleanup_project")
    allowed = {
        "volume": {f"{project}_{volume}" for volume in VOLUMES},
        "network": {f"{project}_default"},
    }
    require(name in allowed.get(kind, set()), "unsafe_cleanup_target")
    require(
        inspected.get("Labels", {}).get("com.docker.compose.project") == project,
        "cleanup_ownership_mismatch",
    )


def validate_compose_boundary(config: dict, project: str, source: Path | None = None) -> None:
    """A selected commit cannot redirect cleanup or mount a caller directory."""
    require(set(config["volumes"]) == set(VOLUMES), "unexpected_compose_volumes")
    for key in VOLUMES:
        volume = config["volumes"][key]
        require(
            volume.get("name") == f"{project}_{key}"
            and not volume.get("external")
            and volume.get("driver", "local") == "local"
            and not volume.get("driver_opts"),
            "unsafe_compose_volume",
        )
    require(
        set(config["networks"]) == {"default"}
        # Compose omits false boolean defaults from canonical JSON output.
        and config["networks"]["default"].get("internal", False) is False
        and config["networks"]["default"].get("name") == f"{project}_default"
        and config["networks"]["default"].get("driver", "bridge") == "bridge"
        and not config["networks"]["default"].get("external"),
        "web_ingress_network_must_be_scoped",
    )
    require(
        config["networks"]["default"].get("driver_opts")
        == {"com.docker.network.bridge.gateway_mode_ipv4": "nat"},
        "web_ingress_network_requires_nat_gateway",
    )
    require(
        set(config["services"])
        == {"api", "web", "run-worker", "scheduler-worker", "local-secret-init", "migrate-seed"},
        "unexpected_compose_services",
    )
    require(not config.get("secrets") and not config.get("configs"), "external_config_forbidden")
    for name, service in config["services"].items():
        expected_image = f"{project}-{'web' if name == 'web' else 'backend'}:local"
        require(service.get("image") == expected_image, "image_tag_outside_scoped_project")
        require(
            not service.get("privileged")
            and not service.get("devices")
            and not service.get("cap_add"),
            "privileged_service_forbidden",
        )
        if name == "web":
            require(
                service.get("network_mode") is None
                and set(service.get("networks", {})) == {"default"},
                "web_requires_only_scoped_ingress_network",
            )
        else:
            require(
                service.get("network_mode") == "none" and not service.get("networks"),
                "backend_requires_network_none",
            )
        require(not service.get("extra_hosts"), "extra_host_mapping_forbidden")
        require(not service.get("env_file"), "service_environment_file_forbidden")
        if source is not None and service.get("build"):
            build = service["build"]
            require(
                Path(build["context"]).resolve() == source.resolve()
                and not build.get("additional_contexts"),
                "build_outside_tracked_export",
            )
        expected_mounts = (
            {("api-socket", "/var/run/marketing-agents", True)}
            if name == "web"
            else {
                ("data", "/var/lib/marketing-agents/data", name == "local-secret-init"),
                ("local-secrets", "/var/lib/marketing-agents/secrets", name != "local-secret-init"),
            }
        )
        if name == "api":
            expected_mounts.add(("api-socket", "/var/run/marketing-agents", False))
            require(
                service.get("environment", {}).get("MARKETING_AGENTS_API_SOCKET") == API_SOCKET,
                "api_requires_fixed_local_socket",
            )
        actual_mounts = set()
        for mount in service.get("volumes", []):
            require(
                mount.get("type") == "volume" and mount.get("source") in VOLUMES,
                "caller_bind_mount_forbidden",
            )
            actual_mounts.add(
                (mount.get("source"), mount.get("target"), bool(mount.get("read_only")))
            )
        require(actual_mounts == expected_mounts, "unexpected_service_mounts")
        ports = service.get("ports", [])
        require(len(ports) == (1 if name == "web" else 0), "unexpected_service_publication")
        for port in ports:
            require(
                name == "web"
                and port.get("host_ip") == "127.0.0.1"
                and port.get("target") == 8080
                and port.get("protocol", "tcp") == "tcp"
                and re.fullmatch(r"[0-9]{4,5}", str(port.get("published")))
                and 1024 <= int(port["published"]) <= 65535,
                "non_loopback_publication_forbidden",
            )


def validate_container_network(inspected: dict, service: str, project: str, port: int) -> None:
    labels = inspected.get("Config", {}).get("Labels", {})
    require(
        labels.get("com.docker.compose.project") == project
        and labels.get("com.docker.compose.service") == service,
        "runtime_container_ownership_mismatch",
    )
    network_mode = inspected.get("HostConfig", {}).get("NetworkMode")
    networks = inspected.get("NetworkSettings", {}).get("Networks", {})
    ports = inspected.get("NetworkSettings", {}).get("Ports", {})
    if service == "web":
        require(
            network_mode == f"{project}_default" and set(networks) == {f"{project}_default"},
            "runtime_web_network_mismatch",
        )
        require(
            ports == {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(port)}]},
            "runtime_loopback_port_not_published",
        )
    else:
        require(service in BACKENDS and network_mode == "none", "runtime_backend_network_not_none")
        require(not ports and set(networks).issubset({"none"}), "runtime_backend_network_attached")


class Verification:
    def __init__(self, repository: Path, ref: str, report: Path | None = None) -> None:
        validate_ref(ref)
        self.repository = repository.resolve()
        self.ref = ref
        self.report_path = report
        self.project = "marketing-agents-del05-" + uuid.uuid4().hex[:16]
        self.environment = clean_environment(dict(os.environ))
        self.temporary: Path | None = None
        self.source: Path | None = None
        self.compose: list[str] = []
        self.created = False
        self.cleanup_deadline: float | None = None
        self.test_containers: list[str] = []
        self.report: dict = {
            "schema_version": 1,
            "project": self.project,
            "ok": False,
            "phases": [],
            "commands": [],
            "cleanup": {"ok": False},
            "deadlines": {
                "execution_seconds": EXECUTION_SECONDS,
                "docker_cleanup_seconds": CLEANUP_SECONDS,
            },
        }

    def command(
        self,
        label: str,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = 300,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        started = time.monotonic()
        command_timeout: float = timeout
        if self.cleanup_deadline is not None:
            remaining = self.cleanup_deadline - started
            require(remaining > 0, "cleanup_deadline_exceeded")
            command_timeout = min(command_timeout, remaining)
        try:
            if label == "offline-backend":
                result = self._offline_command(args, cwd=cwd, timeout=command_timeout)
            else:
                result = subprocess.run(
                    args,
                    cwd=cwd or self.source or self.repository,
                    env=self.environment,
                    capture_output=True,
                    timeout=command_timeout,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            entry = {
                "label": label,
                "status": "timeout",
                "timeout": command_timeout,
                "seconds": round(time.monotonic() - started, 3),
                "stdout_sha256": hashlib.sha256(exc.stdout or b"").hexdigest(),
                "stderr_sha256": hashlib.sha256(exc.stderr or b"").hexdigest(),
            }
            if label == "offline-backend":
                entry["offline_diagnostics"] = project_diagnostics(
                    exc.stdout, exc.stderr, self.source or self.repository
                )
            self.report["commands"].append(entry)
            raise VerificationFailure(f"command_timeout:{label}") from None
        except VerificationFailure as exc:
            if label == "offline-backend":
                stdout = getattr(exc, "stdout", b"")
                stderr = getattr(exc, "stderr", b"")
                self.report["commands"].append(
                    {
                        "label": label,
                        "status": "interrupted",
                        "seconds": round(time.monotonic() - started, 3),
                        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                        "offline_diagnostics": project_diagnostics(
                            stdout, stderr, self.source or self.repository
                        ),
                    }
                )
            raise
        entry = {
            "label": label,
            "argv": args,
            "returncode": result.returncode,
            "seconds": round(time.monotonic() - started, 3),
            "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
        }
        if label == "offline-backend":
            entry["offline_diagnostics"] = project_diagnostics(
                result.stdout, result.stderr, self.source or self.repository
            )
        self.report["commands"].append(entry)
        require(not check or result.returncode == 0, f"command_failed:{label}")
        return result

    def _offline_command(
        self, args: list[str], *, cwd: Path | None, timeout: float
    ) -> subprocess.CompletedProcess:
        # Anonymous, private temporary files keep partial evidence available when
        # the aggregate SIGALRM interrupts subprocess.run. They are never named
        # in reports or retained as artifacts; only the allowlisted projection
        # and hashes survive. Existing execution/cleanup deadlines are unchanged.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                result = subprocess.run(
                    args,
                    cwd=cwd or self.source or self.repository,
                    env=self.environment,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=timeout,
                    check=False,
                )
            except (subprocess.TimeoutExpired, VerificationFailure) as exc:
                stdout.seek(0)
                stderr.seek(0)
                exc.stdout = getattr(exc, "stdout", None) or stdout.read()
                exc.stderr = getattr(exc, "stderr", None) or stderr.read()
                raise
            stdout.seek(0)
            stderr.seek(0)
            return subprocess.CompletedProcess(
                result.args,
                result.returncode,
                result.stdout if result.stdout is not None else stdout.read(),
                result.stderr if result.stderr is not None else stderr.read(),
            )

    def compose_command(
        self, label: str, *args: str, timeout: int = 300
    ) -> subprocess.CompletedProcess:
        return self.command(label, [*self.compose, *args], timeout=timeout)

    def json_command(self, label: str, args: list[str]) -> dict:
        result = json.loads(self.command(label, args).stdout)
        require(isinstance(result, dict) and result.get("ok", True), f"invalid_result:{label}")
        return result

    def snapshot(self, label: str) -> dict:
        return self.json_command(
            label,
            [
                *self.compose,
                "exec",
                "-T",
                "api",
                "python",
                "scripts/del_05_runtime_smoke.py",
                "snapshot",
                "--database",
                DATABASE,
                "--key",
                KEY,
            ],
        )

    def fixture(self, command: str) -> dict:
        return self.json_command(
            f"deployed-replay-fixture-{command}",
            [
                *self.compose,
                "exec",
                "-T",
                "api",
                "python",
                "scripts/del_05_replay_fixture.py",
                command,
            ],
        )

    def phase(self, name: str) -> None:
        self.report["phases"].append(name)
        print(f"DEL-05 phase: {name}", flush=True)

    def prepare(self) -> None:
        require(shutil.which("git") and shutil.which("docker"), "git_and_docker_required")
        for name in (
            "COMPOSE_FILE",
            "COMPOSE_PROJECT_NAME",
            "COMPOSE_PROFILES",
            "DATABASE_URL",
            "MARKETING_AGENTS_DIGEST_KEY_PATH",
        ):
            require(not os.environ.get(name), f"caller_override_forbidden:{name}")
        if self.environment.get("DOCKER_CONTEXT") or not self.environment.get("DOCKER_HOST"):
            contexts = json.loads(
                self.command(
                    "inspect-local-docker-context", ["docker", "context", "inspect"]
                ).stdout
            )
            require(len(contexts) == 1, "ambiguous_docker_context")
            endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
        else:
            endpoint = self.environment["DOCKER_HOST"]
        # Pin the resolved local engine for the entire run, including cleanup.
        self.environment["DOCKER_HOST"] = validate_docker_endpoint(endpoint)
        self.environment.pop("DOCKER_CONTEXT", None)
        self.report["docker_engine_transport"] = "local-unix-socket"
        engine_version = json.loads(
            self.command(
                "verify-docker-engine-version",
                ["docker", "version", "--format", "{{json .Server.Version}}"],
            ).stdout
        )
        validate_docker_engine_version(engine_version)
        self.report["docker_engine_version"] = engine_version
        commit = (
            self.command(
                "resolve-selected-commit",
                ["git", "rev-parse", "--verify", f"{self.ref}^{{commit}}"],
                cwd=self.repository,
            )
            .stdout.decode()
            .strip()
        )
        require(re.fullmatch(r"[0-9a-f]{40,64}", commit), "invalid_commit_identity")
        self.report["source_commit"] = commit
        dirty = self.command(
            "caller-worktree-status", ["git", "status", "--porcelain=v1", "-z"], cwd=self.repository
        ).stdout
        self.report["caller_worktree_dirty"] = bool(dirty)
        self.caller_status = dirty
        self.temporary = Path(tempfile.mkdtemp(prefix="marketing-agents-del05-")).resolve()
        self.temporary.chmod(0o700)
        self.source = self.temporary / "source"
        self.source.mkdir()
        archive = self.temporary / "source.tar"
        self.command(
            "archive-selected-commit",
            ["git", "archive", "--format=tar", f"--output={archive}", commit],
            cwd=self.repository,
        )
        export_archive(archive, self.source)
        require(
            all((self.source / name).is_file() for name in REQUIRED),
            "selected_commit_missing_inputs",
        )
        self.source_hash = tree_fingerprint(self.source)
        self.report["tracked_export_sha256"] = self.source_hash
        self.report["locks"] = {
            name: hashlib.sha256((self.source / name).read_bytes()).hexdigest()
            for name in ("uv.lock", "pnpm-lock.yaml")
        }
        self.report["acquisition"] = {
            "registry_access_permitted": True,
            "builder": "default-local-daemon",
            "base_image_references": sorted(
                {
                    line.split()[1]
                    for kind in ("api", "web")
                    for line in (self.source / f"docker/{kind}.Dockerfile").read_text().splitlines()
                    if line.startswith("FROM ") and "@sha256:" in line
                }
            ),
            "application_provider_egress_permitted": False,
        }
        self.report["network_boundary"] = {
            "backend_external_egress": "denied-by-network-none",
            "web_ingress": "scoped-nat-bridge-loopback-publication",
            "web_external_route_present": True,
            "web_upstream": "fixed-local-unix-socket",
            "web_sensitive_storage_mounted": False,
            "all_container_egress_denied": False,
            "offline_test_containers": "network-none",
            "browser_container": "shares-web-ingress-network-namespace",
        }
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            port = port_probe.getsockname()[1]
        self.environment.update(
            {
                "COMPOSE_PROJECT_NAME": self.project,
                "MARKETING_AGENTS_WEB_PORT": str(port),
                "MARKETING_AGENTS_SOURCE_REVISION": commit,
            }
        )
        self.origin = f"http://127.0.0.1:{port}"
        self.compose = [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--project-directory",
            str(self.source),
            "--env-file",
            "/dev/null",
            "-f",
            str(self.source / "compose.yaml"),
        ]
        override = self.temporary / "verification.compose.json"
        override.write_text(
            json.dumps(
                {
                    "services": {
                        service: {
                            "environment": {
                                "DEL05_VERIFICATION_SCOPE": self.project,
                                "WEBHOOK_HMAC_SECRET": PUBLIC_FIXTURE_HMAC,
                            }
                        }
                        for service in ("api", "run-worker", "scheduler-worker")
                    }
                }
            )
        )
        self.compose.extend(["-f", str(override)])
        config = json.loads(
            self.compose_command("validate-compose", "config", "--format", "json").stdout
        )
        validate_compose_boundary(config, self.project, self.source)
        for name in (f"{self.project}_{volume}" for volume in VOLUMES):
            existing = self.command(
                "ensure-fresh-volume", ["docker", "volume", "inspect", name], check=False
            )
            require(existing.returncode != 0, "test_volume_already_exists")

    def inspect_runtime_network(self) -> None:
        port = urlsplit(self.origin).port
        require(port is not None, "missing_runtime_loopback_port")
        for service in ("web", *BACKENDS):
            container_id = (
                self.compose_command(f"resolve-runtime-{service}", "ps", "--all", "-q", service)
                .stdout.decode()
                .strip()
            )
            require(
                re.fullmatch(r"[0-9a-f]{12,64}", container_id), "invalid_runtime_container_identity"
            )
            inspected = json.loads(
                self.command(
                    f"inspect-runtime-{service}", ["docker", "container", "inspect", container_id]
                ).stdout
            )
            require(len(inspected) == 1, "ambiguous_runtime_container_identity")
            validate_container_network(inspected[0], service, self.project, port)
        # Probe only the running backend processes. Initializers have already
        # exited; their actual HostConfig.NetworkMode is checked above instead.
        probe = (
            "import errno,socket; s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); "
            "s.settimeout(2); result=s.connect_ex(('192.0.2.1',443)); s.close(); "
            "assert result in (errno.ENETUNREACH,errno.EHOSTUNREACH), 'unexpected_external_route'"
        )
        for service in ("api", "run-worker", "scheduler-worker"):
            self.compose_command(
                f"probe-no-external-route-{service}", "exec", "-T", service, "python", "-c", probe
            )
        self.report["network_boundary"]["runtime_network_modes_inspected"] = True
        self.report["network_boundary"]["backend_external_route_probes_failed_closed"] = True
        self.report["network_boundary"]["actual_loopback_port_publication_verified"] = True

    def run_offline(self, image: str, command: list[str], suffix: str) -> None:
        name = f"{self.project}-{suffix}"
        self.test_containers.append(name)
        self.command(
            f"offline-{suffix}",
            [
                "docker",
                "run",
                "--rm",
                "--name",
                name,
                "--label",
                f"com.docker.compose.project={self.project}",
                "--network",
                "none",
                "--env",
                "CI=1",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                image,
                *command,
            ],
            timeout=1800,
        )

    def execute(self) -> None:
        self.phase("tracked-source-export")
        self.prepare()
        self.phase("registry-acquisition-and-frozen-build")
        self.compose_command(
            "build-runtime-images", "build", "--builder", "default", "--pull", timeout=1800
        )
        for kind in ("api", "web"):
            self.command(
                f"build-{kind}-verification-image",
                [
                    "docker",
                    "build",
                    "--builder",
                    "default",
                    "--pull",
                    "--target",
                    "verification",
                    "--file",
                    f"docker/{kind}.Dockerfile",
                    "--tag",
                    f"{self.project}-{kind}-verification",
                    ".",
                ],
                timeout=1800,
            )
        self.phase("backend-no-egress-and-web-ingress-fresh-start")
        self.created = True
        self.compose_command(
            "start-fresh-runtime",
            "up",
            "--detach",
            "--no-build",
            "--pull",
            "never",
            "--wait",
            "--wait-timeout",
            "180",
            timeout=240,
        )
        self.inspect_runtime_network()
        helper = str(self.source / "scripts/del_05_runtime_smoke.py")
        self.report["startup"] = self.json_command(
            "ready-session-counts", ["python3", helper, "ready", "--origin", self.origin]
        )
        self.phase("idempotent-seed")
        self.compose_command(
            "quiesce-workers-before-reseed", "stop", "run-worker", "scheduler-worker"
        )
        self.report["replay_fixture"] = self.fixture("prepare")
        before_seed = self.snapshot("snapshot-before-reseed")
        seed = self.json_command(
            "repeat-catalog-seed",
            [
                *self.compose,
                "exec",
                "-T",
                "api",
                "python",
                "-m",
                "marketing_agents.workers.database_cli",
                "seed",
            ],
        )
        require(
            all(
                seed.get(key) == 0
                for key in ("inserted", "updated", "deleted", "configuration_inserted")
            ),
            "reseed_performed_writes",
        )
        require(
            before_seed == self.snapshot("snapshot-after-reseed"), "reseed_changed_database_or_key"
        )
        self.report["reseed"] = {
            "zero_writes": True,
            "configuration_preserved": seed["configuration_preserved"],
        }
        self.compose_command(
            "load-verification-bindings", "restart", "api", "run-worker", "scheduler-worker"
        )
        self.phase("deployed-five-demo-approval-smoke")
        demos = self.json_command(
            "deployed-demos", ["python3", helper, "demos", "--origin", self.origin]
        )
        self.report["runtime"] = demos
        previous = self.temporary / "demo-summary.json"
        previous.write_text(json.dumps(demos))
        self.phase("deployed-webhook-and-scheduler-admission")
        before_ingress = self.fixture("deliver")
        require(before_ingress["webhook_disposition"] == "created", "webhook_fixture_not_fresh")
        self.compose_command("quiesce-before-restart", "stop", "run-worker", "scheduler-worker")
        before_restart = self.snapshot("snapshot-before-restart")
        self.compose_command(
            "restart-api-and-workers", "restart", "api", "run-worker", "scheduler-worker"
        )
        self.report["restart"] = self.json_command(
            "replay-after-restart",
            ["python3", helper, "replay", "--origin", self.origin, "--previous", str(previous)],
        )
        after_ingress = self.fixture("deliver")
        require(after_ingress["webhook_disposition"] == "replayed", "webhook_restart_not_replayed")
        for field in (
            "webhook_receipt_id",
            "webhook_work_id",
            "webhook_run_id",
            "schedule_occurrence_id",
            "schedule_work_id",
            "schedule_run_id",
        ):
            require(before_ingress[field] == after_ingress[field], "restart_duplicate_ingress")
        self.report["deployed_ingress_restart"] = after_ingress
        self.compose_command("quiesce-after-restart", "stop", "run-worker", "scheduler-worker")
        after_restart = self.snapshot("snapshot-after-restart")
        require(
            before_restart["key_fingerprint"] == after_restart["key_fingerprint"]
            and before_restart["counts"] == after_restart["counts"],
            "restart_identity_or_count_drift",
        )
        self.report["digest_key_fingerprint"] = after_restart["key_fingerprint"]
        self.phase("prebuilt-backend-and-frontend-no-network-verification")
        # The standalone suites use their own temporary databases/processes.
        # Stop this already-verified deployment so its readiness probes cannot
        # repeatedly compile/inspect the catalog while those suites run.
        self.compose_command("quiesce-runtime-for-offline", "stop", *RUNTIME_SERVICES)
        running = self.compose_command(
            "verify-runtime-quiesced", "ps", "--services", "--status", "running", *RUNTIME_SERVICES
        ).stdout
        require(not running.strip(), "runtime_still_running_during_offline_verification")
        self.report["offline_runtime_isolation"] = {"all_services_stopped": True}
        self.run_offline(
            f"{self.project}-api-verification", ["make", "verify-del-05-offline-backend"], "backend"
        )
        self.run_offline(
            f"{self.project}-web-verification", ["make", "verify-del-05-offline-web"], "frontend"
        )
        self.phase("scoped-loopback-browser-verification")
        self.compose_command(
            "resume-runtime-for-browser",
            "start",
            "--wait",
            "--wait-timeout",
            "180",
            *RUNTIME_SERVICES,
            timeout=240,
        )
        self.inspect_runtime_network()
        resumed = self.json_command(
            "verify-resumed-runtime", ["python3", helper, "ready", "--origin", self.origin]
        )
        require(resumed == self.report["startup"], "resumed_runtime_readiness_drift")
        self.report["offline_runtime_isolation"].update(
            {"all_services_resumed": True, "resumed_readiness_verified": True}
        )
        web_id = (
            self.compose_command("resolve-owned-web-container", "ps", "-q", "web")
            .stdout.decode()
            .strip()
        )
        require(re.fullmatch(r"[0-9a-f]{12,64}", web_id), "invalid_web_container_identity")
        browser_name = f"{self.project}-browser"
        self.test_containers.append(browser_name)
        self.command(
            "production-browser-loopback-only",
            [
                "docker",
                "run",
                "--rm",
                "--name",
                browser_name,
                "--label",
                f"com.docker.compose.project={self.project}",
                "--network",
                f"container:{web_id}",
                "--env",
                "CI=1",
                f"{self.project}-web-verification",
                "node",
                "apps/web/scripts/run-del-05-e2e.mjs",
                "--origin",
                "http://127.0.0.1:8080",
            ],
            timeout=600,
        )
        require(tree_fingerprint(self.source) == self.source_hash, "tracked_export_changed")
        status = self.command(
            "caller-worktree-status-after",
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=self.repository,
        ).stdout
        self.report["caller_worktree_status_preserved"] = status == self.caller_status
        require(status == self.caller_status, "caller_worktree_changed_during_verification")
        self.report["ok"] = True

    def cleanup(self) -> None:
        previous = self.cleanup_deadline
        self.cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
        try:
            self._cleanup_resources()
        finally:
            self.cleanup_deadline = previous

    def _cleanup_resources(self) -> None:
        failures = []
        # Named ephemeral test containers can outlive a killed Docker client.
        for name in self.test_containers:
            try:
                result = self.command(
                    "inspect-test-container", ["docker", "container", "inspect", name], check=False
                )
                if result.returncode == 0:
                    item = json.loads(result.stdout)[0]
                    require(
                        item.get("Config", {}).get("Labels", {}).get("com.docker.compose.project")
                        == self.project,
                        "test_container_ownership_mismatch",
                    )
                    self.command(
                        "remove-owned-test-container",
                        ["docker", "container", "rm", "--force", name],
                    )
            except (VerificationFailure, ValueError, OSError):
                failures.append("test_container_cleanup_failed")
        if self.created:
            try:
                for kind, names in (
                    ("volume", [f"{self.project}_{volume}" for volume in VOLUMES]),
                    ("network", [f"{self.project}_default"]),
                ):
                    for name in names:
                        result = self.command(
                            "inspect-cleanup-resource",
                            ["docker", kind, "inspect", name],
                            check=False,
                        )
                        if result.returncode == 0:
                            validate_owned_resource(
                                kind, name, self.project, json.loads(result.stdout)[0]
                            )
                self.compose_command(
                    "remove-owned-compose-resources",
                    "down",
                    "--volumes",
                    "--timeout",
                    "30",
                    timeout=120,
                )
                for kind in ("volume", "network"):
                    remaining = self.command(
                        "verify-owned-resource-cleanup",
                        [
                            "docker",
                            kind,
                            "ls",
                            "--filter",
                            f"label=com.docker.compose.project={self.project}",
                            "--format",
                            "{{.Name}}",
                        ],
                    )
                    require(not remaining.stdout.strip(), "owned_resources_remain_after_cleanup")
                remaining = self.command(
                    "verify-owned-container-cleanup",
                    [
                        "docker",
                        "container",
                        "ls",
                        "--all",
                        "--filter",
                        f"label=com.docker.compose.project={self.project}",
                        "--format",
                        "{{.ID}}",
                    ],
                )
                require(not remaining.stdout.strip(), "owned_containers_remain_after_cleanup")
            except (VerificationFailure, ValueError, OSError):
                failures.append("compose_cleanup_failed")
        if self.temporary is not None:
            try:
                require(
                    self.temporary.name.startswith("marketing-agents-del05-")
                    and self.temporary.parent == Path(tempfile.gettempdir()).resolve()
                    and not self.temporary.is_symlink(),
                    "unsafe_temporary_cleanup",
                )
                shutil.rmtree(self.temporary)
            except (VerificationFailure, OSError):
                failures.append("temporary_export_cleanup_failed")
        self.report["cleanup"] = {"ok": not failures, "failures": failures}
        if failures:
            self.report["ok"] = False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Committed source to export (default HEAD).")
    parser.add_argument("--report", type=Path, help="New absolute sanitized JSON report path.")
    args = parser.parse_args(argv)
    repository = Path(__file__).resolve().parents[1]
    if args.report is not None:
        require(
            args.report.is_absolute()
            and args.report.parent.is_dir()
            and not args.report.exists()
            and not args.report.is_symlink(),
            "report_must_be_new_absolute_file",
        )
        require(
            not args.report.resolve().is_relative_to(repository),
            "report_must_be_outside_caller_repository",
        )
    verification = Verification(repository, args.ref, args.report)

    with verification_signals(EXECUTION_SECONDS):
        try:
            verification.execute()
        except (VerificationFailure, OSError, ValueError, KeyError) as exc:
            verification.report["failure"] = (
                str(exc)
                if isinstance(exc, VerificationFailure)
                else "verification_contract_failure"
            )
        finally:
            # Execution expiry must not interrupt cleanup. Docker cleanup commands
            # share their own aggregate budget, leaving margin under the 1800s gate.
            signal.setitimer(signal.ITIMER_REAL, 0)
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, signal.SIG_IGN)
            verification.cleanup()
    encoded = json.dumps(verification.report, indent=2, sort_keys=True) + "\n"
    if args.report is not None:
        descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(encoded)
    print(encoded, end="")
    return 0 if verification.report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
