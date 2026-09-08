"""Local startup boundary tests: no Docker daemon or containers are used here."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "scripts" / "compose.sh"

FAKE_DOCKER = """
import json
import os
import sys

arguments = sys.argv[1:]
with open(os.environ["DEL05_FAKE_LOG"], "a") as stream:
    stream.write(json.dumps({"args": arguments, "environment": {
        key: os.environ.get(key) for key in (
            "DOCKER_HOST", "DOCKER_CONTEXT", "COMPOSE_FILE", "COMPOSE_PROFILES",
            "COMPOSE_ENV_FILES", "COMPOSE_PROJECT_NAME", "MARKETING_AGENTS_WEB_PORT",
            "BUILDX_BUILDER"
        )
    }}) + "\\n")
if arguments[:2] == ["compose", "version"]:
    print("Docker Compose version v2.40.0")
elif arguments[:2] == ["context", "inspect"]:
    endpoint = os.environ.get("DEL05_FAKE_CONTEXT", "unix:///tmp/del05-fake.sock")
    if "--format" in arguments:
        print(endpoint)
    else:
        print(json.dumps([{"Endpoints": {"docker": {"Host": endpoint}}}]))
elif arguments[:1] == ["version"]:
    print(os.environ.get("DEL05_FAKE_ENGINE", "28.4.0"))
elif arguments[:1] == ["compose"]:
    sys.exit(int(os.environ.get("DEL05_FAKE_COMPOSE_EXIT", "0")))
else:
    sys.exit(91)
"""


@pytest.fixture
def run_wrapper(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    executable = binary / "docker"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
    executable.chmod(0o700)
    log = tmp_path / "docker-calls.jsonl"

    def invoke(action="config", **overrides):
        if log.exists():
            log.unlink()
        environment = {
            "PATH": f"{binary}:{os.defpath}",
            "HOME": str(tmp_path),
            "DEL05_FAKE_LOG": str(log),
            **overrides,
        }
        result = subprocess.run(
            ["/bin/sh", str(WRAPPER), action],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    return invoke


def compose_operations(calls):
    return [
        call
        for call in calls
        if call["args"][:1] == ["compose"] and call["args"][:2] != ["compose", "version"]
    ]


def test_del_05_compose_pins_local_builder_despite_inherited_selection(run_wrapper):
    result, calls = run_wrapper("up", BUILDX_BUILDER="remote-builder")
    assert result.returncode == 0
    operations = compose_operations(calls)
    assert operations
    assert all(call["environment"]["BUILDX_BUILDER"] == "default" for call in operations)


@pytest.mark.parametrize(
    "project",
    [
        "unrelated",
        "marketing-agents-",
        "marketing-agents-../other",
        "marketing-agents-$(unsafe)",
        "marketing-agents-" + "a" * 65,
    ],
)
def test_del_05_compose_refuses_broad_or_invalid_project_before_docker(run_wrapper, project):
    result, calls = run_wrapper(COMPOSE_PROJECT_NAME=project)
    assert result.returncode == 2
    assert calls == []


@pytest.mark.parametrize("port", ["0", "80", "65536", "-1", "not-an-integer", "9" * 50])
def test_del_05_compose_refuses_invalid_or_overflowing_ports(run_wrapper, port):
    result, calls = run_wrapper(MARKETING_AGENTS_WEB_PORT=port)
    assert result.returncode == 2
    assert calls == []


@pytest.mark.parametrize(
    "endpoint", ["tcp://127.0.0.1:2375", "ssh://remote.example", "tcp://remote.example:2376"]
)
def test_del_05_compose_refuses_non_unix_engine(run_wrapper, endpoint):
    result, calls = run_wrapper(DOCKER_HOST=endpoint)
    assert result.returncode == 2
    assert compose_operations(calls) == []


def test_del_05_compose_honors_context_precedence_before_enforcing_local_engine(run_wrapper):
    result, calls = run_wrapper(
        DOCKER_HOST="unix:///tmp/local.sock",
        DOCKER_CONTEXT="remote",
        DEL05_FAKE_CONTEXT="ssh://remote.example",
    )
    assert result.returncode == 2
    assert compose_operations(calls) == []


def test_del_05_compose_pins_resolved_local_endpoint_and_scrubs_overrides(run_wrapper):
    result, calls = run_wrapper(
        DOCKER_CONTEXT="local-test",
        COMPOSE_FILE="/outside/compose.yaml",
        COMPOSE_PROFILES="unsafe",
        COMPOSE_ENV_FILES="/outside/.env",
        COMPOSE_PROJECT_NAME="marketing-agents-del05-test",
        MARKETING_AGENTS_WEB_PORT="18080",
    )
    assert result.returncode == 0, result.stderr
    (operation,) = compose_operations(calls)
    assert operation["environment"]["DOCKER_HOST"] == "unix:///tmp/del05-fake.sock"
    assert operation["environment"]["DOCKER_CONTEXT"] is None
    for name in ("COMPOSE_FILE", "COMPOSE_PROFILES", "COMPOSE_ENV_FILES"):
        assert operation["environment"][name] is None
    arguments = operation["args"]
    for flag, expected in (
        ("--env-file", "/dev/null"),
        ("--project-name", "marketing-agents-del05-test"),
        ("--project-directory", str(ROOT)),
        ("--file", str(ROOT / "compose.yaml")),
    ):
        assert arguments[arguments.index(flag) + 1] == expected


@pytest.mark.parametrize("version", ["27.5.0", "unknown", ""])
def test_del_05_compose_requires_engine_with_correct_loopback_publication(run_wrapper, version):
    result, calls = run_wrapper(DEL05_FAKE_ENGINE=version)
    assert result.returncode == 2
    assert compose_operations(calls) == []


def test_del_05_compose_down_preserves_key_and_database_volumes(run_wrapper):
    result, calls = run_wrapper("down")
    assert result.returncode == 0
    (operation,) = compose_operations(calls)
    assert operation["args"][-3:] == ["down", "--timeout", "40"]
    assert not {"--volumes", "--remove-orphans", "--rmi"}.intersection(operation["args"])


def test_del_05_compose_up_has_bounded_readiness_and_propagates_failure(run_wrapper):
    result, calls = run_wrapper("up", DEL05_FAKE_COMPOSE_EXIT="42")
    assert result.returncode == 42
    assert "Marketing Agents: http" not in result.stdout
    (operation,) = compose_operations(calls)
    assert operation["args"][-6:] == [
        "up",
        "--build",
        "--detach",
        "--wait",
        "--wait-timeout",
        "180",
    ]


def test_del_05_compose_declares_web_ingress_and_network_none_backends():
    config = yaml.safe_load((ROOT / "compose.yaml").read_text())
    services = config["services"]
    assert set(services) == {
        "web",
        "api",
        "run-worker",
        "scheduler-worker",
        "local-secret-init",
        "migrate-seed",
    }
    assert set(config["volumes"]) == {"data", "local-secrets", "api-socket"}
    assert config["networks"]["default"]["internal"] is False
    assert config["networks"]["default"]["driver_opts"] == {
        "com.docker.network.bridge.gateway_mode_ipv4": "nat"
    }
    for name in ("api", "local-secret-init", "migrate-seed", "run-worker", "scheduler-worker"):
        assert services[name]["network_mode"] == "none"
        assert not services[name].get("networks")
    assert not services["web"].get("network_mode")
    assert services["web"]["volumes"] == ["api-socket:/var/run/marketing-agents:ro"]
    assert services["web"]["group_add"] == ["10001"]
    assert (
        services["api"]["environment"]["MARKETING_AGENTS_API_SOCKET"]
        == "/var/run/marketing-agents/api.sock"
    )
    assert "api-socket:/var/run/marketing-agents" in services["api"]["volumes"]
    for name, service in services.items():
        assert service["read_only"]
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert not service["user"].startswith("0:")
        if name == "web":
            assert len(service["ports"]) == 1
            assert service["ports"][0]["host_ip"] == "127.0.0.1"
        else:
            assert not service.get("ports")
            assert service["environment"]["ALLOW_EXTERNAL_NETWORK"] == "false"
            assert (
                service["environment"]["LLM_PROVIDER"]
                == service["environment"]["CONNECTOR_MODE"]
                == "mock"
            )
            secrets = next(
                mount for mount in service["volumes"] if mount.startswith("local-secrets:")
            )
            assert secrets.endswith(":ro") is (name != "local-secret-init")
    assert (
        services["migrate-seed"]["depends_on"]["local-secret-init"]["condition"]
        == "service_completed_successfully"
    )
    for name in ("api", "run-worker", "scheduler-worker", "web"):
        assert (
            services[name]["depends_on"]["migrate-seed"]["condition"]
            == "service_completed_successfully"
        )


def test_del_05_proxy_targets_fixed_local_unix_socket_and_uses_header_allowlist():
    proxy = (ROOT / "docker/web.conf").read_text()
    assert "proxy_pass http://unix:/var/run/marketing-agents/api.sock;" in proxy
    assert len(re.findall(r"\bproxy_pass\s", proxy)) == 1
    assert not re.search(r"\bresolver\s", proxy)
    assert "proxy_pass_request_headers off;" in proxy
    assert "if ($local_host = 0) { return 400; }" in proxy
    forwarded = [
        line.strip().split()[1].casefold()
        for line in proxy.splitlines()
        if line.strip().startswith("proxy_set_header ")
    ]
    assert "origin" in forwarded and "x-csrf-token" in forwarded
    assert "x-webhook-signature" in forwarded
    assert all(name != "forwarded" and not name.startswith("x-forwarded-") for name in forwarded)


def test_del_05_dockerfiles_pin_external_bases_and_use_frozen_dependencies():
    for kind in ("api", "web"):
        recipe = (ROOT / f"docker/{kind}.Dockerfile").read_text()
        stages = set()
        for line in recipe.splitlines():
            if not line.startswith("FROM "):
                continue
            words = line.split()
            if words[1] not in stages:
                assert re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", words[1])
            if "AS" in words:
                stages.add(words[words.index("AS") + 1])
        assert {"verification", "runtime"}.issubset(stages)
    backend = (ROOT / "docker/api.Dockerfile").read_text()
    web = (ROOT / "docker/web.Dockerfile").read_text()
    assert "uv sync --frozen --no-dev --no-install-project" in backend
    assert "pnpm install --frozen-lockfile" in web
    assert f"node:{(ROOT / '.nvmrc').read_text().strip().removeprefix('v')}-" in web
    backend_runtime = backend.split("FROM base AS runtime\n", 1)[1]
    assert "USER 10001:10001" in backend_runtime
    assert "ALLOW_EXTERNAL_NETWORK=false" in backend_runtime
    assert "REAL_LLM_OPT_IN=false REAL_CONNECTOR_OPT_IN=false" in backend_runtime
    assert "USER 101:101" in web.rsplit(" AS runtime\n", 1)[1]
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    for excluded in (".git", ".env", ".env.*", "*.key", "*.pem", "*.db", "node_modules", ".venv"):
        assert excluded in ignored or "**/" + excluded in ignored
