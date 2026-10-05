"""AC-01 environment contracts reject ambient credentials and runtime drift."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from scripts import del_05_clean_state as clean_state
from scripts.ac_01_environment import (
    API_SOCKET,
    BACKEND_SERVICES,
    FIXTURE_SERVICES,
    PUBLIC_FIXTURE_HMAC,
    SERVICES,
    EnvironmentBoundaryError,
    ImageEnvironmentBaseline,
    validate_compose_service_environments,
    validate_runtime_container_environment,
    verify_image_environment,
)
from scripts.del_05_clean_state import PUBLIC_FIXTURE_HMAC as CLEAN_STATE_FIXTURE_HMAC
from scripts.del_05_replay_fixture import PUBLIC_FIXTURE_HMAC as REPLAY_FIXTURE_HMAC

PROJECT = "marketing-agents-del05-0123456789abcdef"
PORT = 18080
BACKEND_IMAGE_ID = "sha256:" + "a" * 64
WEB_IMAGE_ID = "sha256:" + "b" * 64

BACKEND_IMAGE_ENVIRONMENT = {
    "PATH": (
        "/app/.venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    ),
    "LANG": "C.UTF-8",
    "GPG_KEY": "7169605F62C751356D054A26A821E680E5FA6305",
    "PYTHON_VERSION": "3.12.14",
    "PYTHON_SHA256": "5c8462af5790baf43a321a1559dbe0db06d1be4300fb85fb53c40060668e548a",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONPATH": "/app/apps/api/src:/app",
    "APP_ENV": "local",
    "AUTH_MODE": "local",
    "LLM_PROVIDER": "mock",
    "CONNECTOR_MODE": "mock",
    "ALLOW_EXTERNAL_NETWORK": "false",
    "REAL_LLM_OPT_IN": "false",
    "REAL_CONNECTOR_OPT_IN": "false",
    "DATABASE_URL": "sqlite+aiosqlite:////var/lib/marketing-agents/data/marketing_agents.db",
    "MARKETING_AGENTS_DIGEST_KEY_PATH": "/var/lib/marketing-agents/secrets/digest.key",
    "CATALOG_ROOT": "/app/catalog/v1",
}
WEB_IMAGE_ENVIRONMENT = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "NGINX_VERSION": "1.30.4",
    "PKG_RELEASE": "1",
    "DYNPKG_RELEASE": "1",
    "NJS_VERSION": "1.0.1",
    "NJS_RELEASE": "1",
}
BASE_COMPOSE_ENVIRONMENT = {
    "APP_ENV": "local",
    "AUTH_MODE": "local",
    "LLM_PROVIDER": "mock",
    "CONNECTOR_MODE": "mock",
    "ALLOW_EXTERNAL_NETWORK": "false",
    "REAL_LLM_OPT_IN": "false",
    "REAL_CONNECTOR_OPT_IN": "false",
    "DATABASE_URL": "sqlite+aiosqlite:////var/lib/marketing-agents/data/marketing_agents.db",
    "MARKETING_AGENTS_DIGEST_KEY_PATH": "/var/lib/marketing-agents/secrets/digest.key",
    "CATALOG_ROOT": "/app/catalog/v1",
    "API_HOST": "127.0.0.1",
    "API_PORT": "8000",
    "API_TRUSTED_ORIGINS": ('["http://127.0.0.1:18080","http://localhost:18080"]'),
}


def environment_entries(environment: dict[str, str]) -> list[str]:
    return [f"{name}={value}" for name, value in environment.items()]


def image_inspection(image_kind: str) -> dict[str, object]:
    if image_kind == "backend":
        return {
            "Id": BACKEND_IMAGE_ID,
            "Config": {"Env": environment_entries(BACKEND_IMAGE_ENVIRONMENT)},
        }
    return {"Id": WEB_IMAGE_ID, "Config": {"Env": environment_entries(WEB_IMAGE_ENVIRONMENT)}}


def compose_environment(service: str, *, fixture_scope: str | None = None) -> dict[str, str]:
    if service == "web":
        return {}
    environment = dict(BASE_COMPOSE_ENVIRONMENT)
    if service == "api":
        environment["MARKETING_AGENTS_API_SOCKET"] = API_SOCKET
    if fixture_scope is not None and service in FIXTURE_SERVICES:
        environment["DEL05_VERIFICATION_SCOPE"] = fixture_scope
        environment["WEBHOOK_HMAC_SECRET"] = PUBLIC_FIXTURE_HMAC
    return environment


def compose_config(*, fixture_scope: str | None = None) -> dict[str, object]:
    return {
        "services": {
            service: {"environment": compose_environment(service, fixture_scope=fixture_scope)}
            for service in SERVICES
        }
    }


def verified_baselines() -> tuple[ImageEnvironmentBaseline, ImageEnvironmentBaseline]:
    return (
        verify_image_environment(image_inspection("backend"), image_kind="backend"),
        verify_image_environment(image_inspection("web"), image_kind="web"),
    )


def runtime_inspection(
    service: str,
    baseline: ImageEnvironmentBaseline,
    *,
    fixture_scope: str | None = None,
) -> dict[str, object]:
    environment = dict(baseline.environment)
    environment.update(compose_environment(service, fixture_scope=fixture_scope))
    return {"Image": baseline.image_id, "Config": {"Env": environment_entries(environment)}}


def completed_json(value: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(value).encode(), stderr=b"")


def runtime_network_inspection(
    service: str,
    *,
    fixture_scope: str | None = None,
    injected_entry: str | None = None,
) -> dict[str, object]:
    image_kind = "web" if service == "web" else "backend"
    baseline = verify_image_environment(image_inspection(image_kind), image_kind=image_kind)
    inspected = runtime_inspection(service, baseline, fixture_scope=fixture_scope)
    config = inspected["Config"]
    assert isinstance(config, dict)
    config["Labels"] = {
        "com.docker.compose.project": PROJECT,
        "com.docker.compose.service": service,
    }
    environment = config["Env"]
    assert isinstance(environment, list)
    if injected_entry is not None:
        environment.append(injected_entry)
    if service == "web":
        network_mode = f"{PROJECT}_default"
        networks = {f"{PROJECT}_default": {}}
        ports = {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(PORT)}]}
    else:
        network_mode = "none"
        networks = {"none": {}}
        ports = {}
    inspected["HostConfig"] = {"NetworkMode": network_mode}
    inspected["NetworkSettings"] = {"Networks": networks, "Ports": ports}
    return inspected


def install_runtime_inspection_fakes(
    verifier: clean_state.Verification,
    monkeypatch: pytest.MonkeyPatch,
    *,
    phase: dict[str, str | None],
    probes: list[str],
    injected_service: str | None = None,
    injected_entry: str | None = None,
) -> None:
    source_commit = "c" * 40
    verifier.project = PROJECT
    verifier.origin = f"http://127.0.0.1:{PORT}"
    verifier.report["source_commit"] = source_commit
    verifier.report["network_boundary"] = {}

    def command(label: str, _arguments: list[str], **_kwargs: object):
        if label.startswith("inspect-runtime-image-"):
            image_kind = label.removeprefix("inspect-runtime-image-")
            inspected = image_inspection(image_kind)
            config = inspected["Config"]
            assert isinstance(config, dict)
            config["Labels"] = {"org.opencontainers.image.revision": source_commit}
            return completed_json([inspected])
        if label.startswith("inspect-runtime-"):
            service = label.removeprefix("inspect-runtime-")
            entry = injected_entry if service == injected_service else None
            return completed_json(
                [
                    runtime_network_inspection(
                        service, fixture_scope=phase["scope"], injected_entry=entry
                    )
                ]
            )
        raise AssertionError(f"unexpected command label: {label}")

    def compose_command(label: str, *_arguments: str, **_kwargs: object):
        if label.startswith("resolve-runtime-"):
            return subprocess.CompletedProcess([], 0, stdout=b"a" * 12 + b"\n", stderr=b"")
        if label.startswith("probe-no-external-route-"):
            probes.append(label)
            return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected compose label: {label}")

    monkeypatch.setattr(verifier, "command", command)
    monkeypatch.setattr(verifier, "compose_command", compose_command)


def test_ac_01_compose_accepts_exact_public_startup_environment() -> None:
    config = compose_config()

    validate_compose_service_environments(config, project=PROJECT, port=PORT)

    services = config["services"]
    assert isinstance(services, dict)
    assert services["web"]["environment"] == {}
    assert all(
        "MARKETING_AGENTS_API_SOCKET" not in services[name]["environment"]
        for name in BACKEND_SERVICES
        if name != "api"
    )
    assert all("DEL05_VERIFICATION_SCOPE" not in services[name]["environment"] for name in SERVICES)


def test_ac_01_compose_accepts_fixture_overlay_only_for_replay_services() -> None:
    config = compose_config(fixture_scope=PROJECT)

    validate_compose_service_environments(config, project=PROJECT, port=PORT, fixture_scope=PROJECT)

    services = config["services"]
    assert isinstance(services, dict)
    for service in SERVICES:
        environment = services[service]["environment"]
        assert ("DEL05_VERIFICATION_SCOPE" in environment) is (service in FIXTURE_SERVICES)
        assert ("WEBHOOK_HMAC_SECRET" in environment) is (service in FIXTURE_SERVICES)


def test_ac_01_public_fixture_literal_matches_harness_and_replay_fixture() -> None:
    assert PUBLIC_FIXTURE_HMAC == CLEAN_STATE_FIXTURE_HMAC == REPLAY_FIXTURE_HMAC


@pytest.mark.parametrize(
    "name",
    [
        "AWS_ACCESS_KEY_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_CLIENT_SECRET",
        "OPENAI_API_KEY",
        "HTTP_PROXY",
        "NODE_OPTIONS",
        "PYTHONPATH",
    ],
)
def test_ac_01_compose_rejects_credential_proxy_and_loader_injections(name: str) -> None:
    config = compose_config()
    secret = "private-value-must-not-reach-diagnostics"
    config["services"]["api"]["environment"][name] = secret

    with pytest.raises(EnvironmentBoundaryError) as caught:
        validate_compose_service_environments(config, project=PROJECT, port=PORT)

    assert caught.value.code == "compose_environment_mismatch:api"
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("name", "unsafe_value"),
    [
        ("APP_ENV", "production"),
        ("AUTH_MODE", "external"),
        ("LLM_PROVIDER", "openai"),
        ("CONNECTOR_MODE", "real"),
        ("ALLOW_EXTERNAL_NETWORK", "true"),
        ("REAL_LLM_OPT_IN", "true"),
        ("REAL_CONNECTOR_OPT_IN", "true"),
    ],
)
def test_ac_01_compose_rejects_real_modes_without_disclosing_values(
    name: str, unsafe_value: str
) -> None:
    config = compose_config()
    config["services"]["run-worker"]["environment"][name] = unsafe_value

    with pytest.raises(EnvironmentBoundaryError, match=r"^compose_environment_mismatch") as caught:
        validate_compose_service_environments(config, project=PROJECT, port=PORT)

    assert unsafe_value not in str(caught.value)


def test_ac_01_compose_binds_port_socket_and_fixture_phase() -> None:
    wrong_port = compose_config()
    with pytest.raises(EnvironmentBoundaryError, match="compose_environment_mismatch"):
        validate_compose_service_environments(wrong_port, project=PROJECT, port=PORT + 1)

    wrong_socket = compose_config()
    wrong_socket["services"]["run-worker"]["environment"]["MARKETING_AGENTS_API_SOCKET"] = (
        API_SOCKET
    )
    with pytest.raises(EnvironmentBoundaryError, match="compose_environment_mismatch"):
        validate_compose_service_environments(wrong_socket, project=PROJECT, port=PORT)

    fixture_during_startup = compose_config(fixture_scope=PROJECT)
    with pytest.raises(EnvironmentBoundaryError, match="compose_environment_mismatch"):
        validate_compose_service_environments(fixture_during_startup, project=PROJECT, port=PORT)


@pytest.mark.parametrize("value", [None, {"BAD-NAME": "x"}, {"APP_ENV": 1}, {"APP_ENV": "x\n"}])
def test_ac_01_compose_rejects_malformed_environment(value: object) -> None:
    config = compose_config()
    config["services"]["api"]["environment"] = value

    with pytest.raises(EnvironmentBoundaryError, match=r"^malformed_compose_environment$"):
        validate_compose_service_environments(config, project=PROJECT, port=PORT)


@pytest.mark.parametrize("image_kind", ["backend", "web"])
def test_ac_01_image_environment_is_exact_and_baseline_is_frozen(image_kind: str) -> None:
    baseline = verify_image_environment(image_inspection(image_kind), image_kind=image_kind)

    assert baseline.image_id.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        baseline.image_id = "sha256:" + "f" * 64


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_SECRET_ACCESS_KEY", "private-cloud-secret"),
        ("HTTPS_PROXY", "http://private-proxy.invalid"),
        ("NODE_OPTIONS", "--require=/outside/loader.js"),
        ("PYTHONPATH", "/outside/python"),
        ("LLM_PROVIDER", "real-provider"),
    ],
)
def test_ac_01_image_rejects_unexpected_and_overridden_environment(name: str, value: str) -> None:
    inspected = image_inspection("backend")
    environment = dict(BACKEND_IMAGE_ENVIRONMENT)
    environment[name] = value
    inspected["Config"]["Env"] = environment_entries(environment)

    with pytest.raises(EnvironmentBoundaryError, match=r"^unsafe_image_environment$") as caught:
        verify_image_environment(inspected, image_kind="backend")

    assert value not in str(caught.value)


@pytest.mark.parametrize(
    ("entries", "code"),
    [
        (["PATH=/one", "PATH=/two"], "duplicate_image_environment"),
        (["MISSING_EQUALS"], "malformed_image_environment"),
        (["=empty-name"], "malformed_image_environment"),
        (["BAD-NAME=value"], "malformed_image_environment"),
        (["NAME=line\nbreak"], "malformed_image_environment"),
        ([1], "malformed_image_environment"),
    ],
)
def test_ac_01_image_rejects_duplicate_and_malformed_entries(
    entries: list[object], code: str
) -> None:
    inspected = image_inspection("backend")
    inspected["Config"]["Env"] = entries

    with pytest.raises(EnvironmentBoundaryError) as caught:
        verify_image_environment(inspected, image_kind="backend")

    assert caught.value.code == code


@pytest.mark.parametrize("fixture_scope", [None, PROJECT])
def test_ac_01_runtime_environment_matches_immutable_image_and_phase(
    fixture_scope: str | None,
) -> None:
    backend, web = verified_baselines()
    for service in SERVICES:
        baseline = web if service == "web" else backend
        inspected = runtime_inspection(service, baseline, fixture_scope=fixture_scope)
        validate_runtime_container_environment(
            inspected,
            service=service,
            baseline=baseline,
            project=PROJECT,
            port=PORT,
            fixture_scope=fixture_scope,
        )


def test_ac_01_runtime_rejects_image_identity_and_kind_mismatches() -> None:
    backend, web = verified_baselines()
    inspected = runtime_inspection("api", backend)
    inspected["Image"] = WEB_IMAGE_ID
    with pytest.raises(EnvironmentBoundaryError, match=r"^runtime_image_identity_mismatch$"):
        validate_runtime_container_environment(
            inspected, service="api", baseline=backend, project=PROJECT, port=PORT
        )

    with pytest.raises(EnvironmentBoundaryError, match=r"^invalid_image_environment_baseline$"):
        validate_runtime_container_environment(
            runtime_inspection("web", web),
            service="web",
            baseline=backend,
            project=PROJECT,
            port=PORT,
        )


def test_ac_01_runtime_rejects_injection_duplicate_and_malformed_entries() -> None:
    backend, _ = verified_baselines()
    injected = runtime_inspection("api", backend)
    secret = "runtime-private-secret"
    injected["Config"]["Env"].append(f"OPENAI_API_KEY={secret}")
    with pytest.raises(EnvironmentBoundaryError, match=r"^runtime_environment_mismatch") as caught:
        validate_runtime_container_environment(
            injected, service="api", baseline=backend, project=PROJECT, port=PORT
        )
    assert secret not in str(caught.value)

    duplicate = runtime_inspection("api", backend)
    duplicate["Config"]["Env"].append("PATH=/outside")
    with pytest.raises(EnvironmentBoundaryError, match=r"^duplicate_runtime_environment$"):
        validate_runtime_container_environment(
            duplicate, service="api", baseline=backend, project=PROJECT, port=PORT
        )

    malformed = runtime_inspection("api", backend)
    malformed["Config"]["Env"] = ["MISSING_EQUALS"]
    with pytest.raises(EnvironmentBoundaryError, match=r"^malformed_runtime_environment$"):
        validate_runtime_container_environment(
            malformed, service="api", baseline=backend, project=PROJECT, port=PORT
        )


def test_ac_01_rejects_forged_baseline_and_wrong_fixture_scope() -> None:
    backend, _ = verified_baselines()
    forged = ImageEnvironmentBaseline(
        image_id=backend.image_id,
        image_kind="backend",
        environment=(*backend.environment, ("OPENAI_API_KEY", "private")),
    )
    with pytest.raises(EnvironmentBoundaryError, match=r"^invalid_image_environment_baseline$"):
        validate_runtime_container_environment(
            runtime_inspection("api", backend),
            service="api",
            baseline=forged,
            project=PROJECT,
            port=PORT,
        )

    config = compose_config(fixture_scope=PROJECT)
    with pytest.raises(EnvironmentBoundaryError, match=r"^invalid_fixture_scope$"):
        validate_compose_service_environments(
            config,
            project=PROJECT,
            port=PORT,
            fixture_scope="marketing-agents-del05-fedcba9876543210",
        )


def test_ac_01_input_mutation_does_not_change_verified_baseline() -> None:
    inspected = image_inspection("backend")
    baseline = verify_image_environment(inspected, image_kind="backend")
    original = deepcopy(baseline.environment)

    inspected["Config"]["Env"].append("OPENAI_API_KEY=late-mutation")

    assert baseline.environment == original


def test_ac_01_clean_state_wires_all_runtime_environments_in_both_phases(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = clean_state.Verification(tmp_path, "HEAD")
    phase: dict[str, str | None] = {"scope": None}
    probes: list[str] = []
    install_runtime_inspection_fakes(verifier, monkeypatch, phase=phase, probes=probes)
    validated: list[tuple[str, str | None]] = []
    real_validator = clean_state.validate_runtime_container_environment

    def validate(inspected, **kwargs):
        validated.append((kwargs["service"], kwargs["fixture_scope"]))
        return real_validator(inspected, **kwargs)

    monkeypatch.setattr(clean_state, "validate_runtime_container_environment", validate)

    verifier.inspect_runtime_network()
    phase["scope"] = PROJECT
    verifier.inspect_runtime_network(fixture_scope=PROJECT)

    service_order = ("web", *clean_state.BACKENDS)
    assert validated == [
        *((service, None) for service in service_order),
        *((service, PROJECT) for service in service_order),
    ]
    assert (
        probes
        == [
            "probe-no-external-route-api",
            "probe-no-external-route-run-worker",
            "probe-no-external-route-scheduler-worker",
        ]
        * 2
    )
    assert verifier.report["environment_boundary"] == {
        "public_runtime_verified": True,
        "fixture_runtime_verified": True,
    }


def test_ac_01_clean_state_runtime_injection_fails_before_probes_and_success_flags(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verifier = clean_state.Verification(tmp_path, "HEAD")
    phase: dict[str, str | None] = {"scope": None}
    probes: list[str] = []
    secret = "runtime-secret-must-remain-redacted"
    install_runtime_inspection_fakes(
        verifier,
        monkeypatch,
        phase=phase,
        probes=probes,
        injected_service="api",
        injected_entry=f"OPENAI_API_KEY={secret}",
    )

    with pytest.raises(EnvironmentBoundaryError) as caught:
        verifier.inspect_runtime_network()

    assert caught.value.code == "runtime_environment_mismatch:api"
    assert secret not in str(caught.value)
    assert probes == []
    assert verifier.report["network_boundary"] == {}
    assert "environment_boundary" not in verifier.report


def test_ac_01_main_redacts_environment_failure_and_always_cleans_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "main-path-secret-must-remain-redacted"
    cleanup_calls: list[str] = []

    def execute(verifier: clean_state.Verification) -> None:
        baseline = verify_image_environment(image_inspection("backend"), image_kind="backend")
        inspected = runtime_inspection("api", baseline)
        config = inspected["Config"]
        assert isinstance(config, dict)
        environment = config["Env"]
        assert isinstance(environment, list)
        environment.append(f"OPENAI_API_KEY={secret}")
        validate_runtime_container_environment(
            inspected,
            service="api",
            baseline=baseline,
            project=verifier.project,
            port=PORT,
        )

    def cleanup(verifier: clean_state.Verification) -> None:
        cleanup_calls.append(verifier.project)
        verifier.report["cleanup"] = {"ok": True, "failures": []}

    monkeypatch.setattr(clean_state.Verification, "execute", execute)
    monkeypatch.setattr(clean_state.Verification, "cleanup", cleanup)

    assert clean_state.main(["--ref", "HEAD"]) == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["failure"] == "runtime_environment_mismatch:api"
    assert report["cleanup"] == {"ok": True, "failures": []}
    assert len(cleanup_calls) == 1
    assert secret not in output
