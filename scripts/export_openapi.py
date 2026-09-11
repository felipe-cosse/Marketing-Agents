"""DEL-07: render the API contract without reading local credentials or starting services."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from marketing_agents.api.app import create_app
from marketing_agents.config import Settings


def contract_document() -> dict[str, object]:
    """Construct route metadata only; no lifespan, database, worker, or connector is started."""
    # This CLI runs in a dedicated short-lived process, never a live API worker.
    # Validate the exact Settings class, without a model_construct bypass or
    # consulting local .env files, credentials, or caller runtime opt-ins.
    with patch.dict(os.environ, {}, clear=True):
        settings = Settings(_env_file=None, app_env="test")
    return create_app(settings=settings).openapi()


def main() -> None:
    print(json.dumps(contract_document(), ensure_ascii=False, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
