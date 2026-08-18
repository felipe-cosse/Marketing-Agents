import unittest

from httpx import ASGITransport, AsyncClient
from marketing_agents.api import create_app
from marketing_agents.config import Settings


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    """Requirement ARCH-01: the FastAPI factory exposes side-effect-free liveness."""

    async def asyncSetUp(self) -> None:
        transport = ASGITransport(app=create_app(Settings(_env_file=None)))
        self.client = AsyncClient(transport=transport, base_url="http://testserver")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_arch_01_liveness_is_typed_and_has_no_dependency_probe(self) -> None:
        response = await self.client.get("/health/live")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok", "service": "marketing-agents-api"}, response.json())

    async def test_arch_01_untrusted_host_is_rejected(self) -> None:
        response = await self.client.get("/health/live", headers={"host": "evil.example"})
        self.assertEqual(400, response.status_code)


if __name__ == "__main__":
    unittest.main()
