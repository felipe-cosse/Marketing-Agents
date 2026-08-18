import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
API_SRC = ROOT / "apps" / "api" / "src"
sys.path.insert(0, str(API_SRC))

from marketing_agents.security.digest_key import (  # noqa: E402
    DigestKeyError,
    digest_key_fingerprint,
    load_or_create_digest_key,
)
from marketing_agents.security.secret_config import REDACTED, SecretValue, redact_config  # noqa: E402


def load_scanner():
    path = ROOT / "scripts" / "scan_secrets.py"
    spec = importlib.util.spec_from_file_location("scan_secrets", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load secret scanner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCANNER = load_scanner()


class SecretHygieneTests(unittest.TestCase):
    """Requirement SAFE-10: local configuration and key material fail closed."""

    def test_safe_10_env_example_is_mock_loopback_and_secret_free(self) -> None:
        values = {}
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value
        self.assertEqual("local", values["APP_ENV"])
        self.assertEqual("local", values["AUTH_MODE"])
        self.assertEqual("mock", values["LLM_PROVIDER"])
        self.assertEqual("mock", values["CONNECTOR_MODE"])
        self.assertEqual("false", values["ALLOW_EXTERNAL_NETWORK"])
        self.assertEqual("127.0.0.1", values["API_HOST"])
        self.assertEqual("", values["REAL_LLM_API_KEY"])

    def test_safe_10_ignore_rules_cover_secrets_state_and_build_outputs(self) -> None:
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for expected in (".env", "*.key", "*.db", ".venv/", "node_modules/", "__pycache__/"):
            self.assertIn(expected, gitignore)
        for expected in (".git", ".env", "*.key", "*.db", ".venv", "node_modules"):
            self.assertIn(expected, dockerignore)
        self.assertIn("!.env.example", gitignore)
        self.assertIn("!.env.example", dockerignore)

    def test_safe_10_config_projection_and_repr_mask_nested_secrets(self) -> None:
        canary = "runtime-only-sensitive-value"
        secret = SecretValue(canary)
        projection = redact_config(
            {
                "provider": {"api_key": canary, "region": "local"},
                "authorization": secret,
                "items": [{"password": canary}],
            }
        )
        self.assertEqual(REDACTED, projection["provider"]["api_key"])
        self.assertEqual("local", projection["provider"]["region"])
        self.assertEqual(REDACTED, projection["authorization"])
        self.assertEqual(REDACTED, projection["items"][0]["password"])
        self.assertNotIn(canary, repr(secret))
        self.assertNotIn(canary, str(secret))

    def test_safe_10_scanner_catches_runtime_canary_and_forbidden_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canary = "AKIA" + ("A" * 16)
            (root / "safe.txt").write_text("safe\n", encoding="utf-8")
            (root / "leak.txt").write_text(canary + "\n", encoding="utf-8")
            (root / ".env").write_text("VALUE=not-a-real-secret\n", encoding="utf-8")
            findings = SCANNER.scan_paths(root, ["safe.txt", "leak.txt", ".env"])
        self.assertEqual(
            {(".env", "forbidden-secret-or-state-file"), ("leak.txt", "aws-access-key")},
            {(finding.path, finding.kind) for finding in findings},
        )

    def test_safe_10_digest_key_is_atomic_private_and_restart_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state" / "digest.key"
            first = load_or_create_digest_key(path)
            original = path.read_bytes()
            second = load_or_create_digest_key(path, persistent_state_exists=True)
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(first.bytes_for_digest(), second.bytes_for_digest())
            self.assertEqual(digest_key_fingerprint(first), digest_key_fingerprint(second))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertNotIn(first.bytes_for_digest().hex(), repr(first))

    def test_safe_10_digest_key_fails_on_missing_mismatch_or_unsafe_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.key"
            with self.assertRaises(DigestKeyError):
                load_or_create_digest_key(missing, persistent_state_exists=True)

            path = root / "digest.key"
            load_or_create_digest_key(path)
            with self.assertRaises(DigestKeyError):
                load_or_create_digest_key(path, expected_fingerprint="digest-key-fingerprint-v1:" + ("0" * 64))
            os.chmod(path, 0o644)
            with self.assertRaises(DigestKeyError):
                load_or_create_digest_key(path)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_safe_10_digest_key_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_text("not a key", encoding="utf-8")
            link = root / "digest.key"
            link.symlink_to(target)
            with self.assertRaises(DigestKeyError):
                load_or_create_digest_key(link)


if __name__ == "__main__":
    unittest.main()
