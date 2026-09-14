"""OBJ-04 dependency-free witness for the actual native process environment boundary."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from scripts import dev


def main() -> None:
    inherited = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/obj04-home",
        "COREPACK_HOME": "/tmp/obj04-corepack-cache",
        "XDG_CACHE_HOME": "/tmp/obj04-xdg-cache",
        "AWS_ACCESS_KEY_ID": "synthetic-obj04-not-a-credential",
        "COREPACK_NPM_TOKEN": "synthetic-obj04-not-a-credential",
        "NODE_OPTIONS": "--require=untrusted",
        "PYTHONPATH": "/untrusted",
        "HTTP_PROXY": "http://example.invalid",
        "COREPACK_ENABLE_NETWORK": "1",
    }
    expected_flags = {
        "COREPACK_ENABLE_NETWORK": "0",
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "COREPACK_ENABLE_AUTO_PIN": "0",
        "PNPM_CONFIG_OFFLINE": "true",
        "PNPM_CONFIG_UPDATE_NOTIFIER": "false",
    }
    forbidden = {
        "AWS_ACCESS_KEY_ID",
        "COREPACK_NPM_TOKEN",
        "NODE_OPTIONS",
        "PYTHONPATH",
        "HTTP_PROXY",
    }
    observed = []

    def observe(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs.get("env")
        assert isinstance(environment, dict), (
            "OBJ-04 version process inherited the host environment"
        )
        assert not forbidden & environment.keys()
        assert all(environment.get(name) == value for name, value in expected_flags.items())
        assert environment["COREPACK_HOME"] == inherited["COREPACK_HOME"]
        assert kwargs["cwd"] == dev.ROOT
        observed.append(environment)
        return subprocess.CompletedProcess(command, 0, stdout="11.24.0\n")

    with (
        patch.dict(os.environ, inherited, clear=True),
        patch.object(dev.subprocess, "run", side_effect=observe),
    ):
        assert dev._version(["/installed/pnpm", "--version"]) == "11.24.0"
        launched = dev.safe_environment(
            Path("/tmp/obj04-installation"),
            dev.Tools(Path("/installed/python"), Path("/installed/node"), Path("/installed/pnpm")),
            api_port=18000,
            web_port=15173,
        )
    assert len(observed) == 1
    assert not forbidden & launched.keys()
    assert all(launched.get(name) == value for name, value in expected_flags.items())
    assert launched["COREPACK_HOME"] == observed[0]["COREPACK_HOME"]
    assert launched["LLM_PROVIDER"] == launched["CONNECTOR_MODE"] == "mock"
    assert launched["ALLOW_EXTERNAL_NETWORK"] == "false"
    print("OBJ-04 passed: credential-free offline prerequisite and supervised environments")


if __name__ == "__main__":
    main()
