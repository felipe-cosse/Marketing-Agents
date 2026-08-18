import unittest

from marketing_agents.config import Settings
from pydantic import ValidationError


class SettingsTests(unittest.TestCase):
    """Requirement ARCH-01: typed settings preserve secure local defaults."""

    def test_arch_01_defaults_are_loopback_mock_and_sqlite(self) -> None:
        settings = Settings(_env_file=None)
        self.assertEqual("127.0.0.1", settings.api_host)
        self.assertEqual("mock", settings.llm_provider)
        self.assertEqual("mock", settings.connector_mode)
        self.assertFalse(settings.allow_external_network)
        self.assertTrue(settings.database_url.startswith("sqlite+aiosqlite://"))

    def test_arch_01_rejects_unsafe_environment_and_network_modes(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, app_env="production")
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, api_host="0.0.0.0")
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, llm_provider="real")
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, unknown_setting="forbidden")

    def test_arch_01_masks_provider_credentials_in_diagnostics(self) -> None:
        canary = "runtime-only-provider-credential"
        settings = Settings(
            _env_file=None,
            llm_provider="real",
            allow_external_network=True,
            real_llm_opt_in=True,
            real_llm_api_key=canary,
        )
        self.assertNotIn(canary, repr(settings))
        self.assertNotIn(canary, str(settings.safe_snapshot()))


if __name__ == "__main__":
    unittest.main()
