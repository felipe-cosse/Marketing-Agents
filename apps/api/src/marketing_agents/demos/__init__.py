"""Public deterministic-demo registry and execution contracts."""

from .composition import (
    SocialContentDraftReadAdapter,
    build_social_content_draft_deterministic_provider,
    build_social_content_draft_read_adapter,
)
from .contracts import (
    DemoScenarioDefinition,
    DemoScenarioInputError,
    DemoScenarioRegistryError,
    DemoScenarioStep,
    DemoSelectedAgent,
)
from .registry import (
    DEMO_SCENARIOS,
    DemoScenarioRegistry,
    build_demo_scenario_registry,
    resolve_demo_input,
)
from .service import DemoRunCommand, DemoRunResult, DemoRunService, DemoRunServiceError
from .social_content_draft import (
    SOCIAL_CONTENT_DRAFT_RENDERER,
    SOCIAL_CONTENT_DRAFT_SCENARIO_ID,
)

__all__ = [
    "DEMO_SCENARIOS",
    "SOCIAL_CONTENT_DRAFT_RENDERER",
    "SOCIAL_CONTENT_DRAFT_SCENARIO_ID",
    "DemoRunCommand",
    "DemoRunResult",
    "DemoRunService",
    "DemoRunServiceError",
    "DemoScenarioDefinition",
    "DemoScenarioInputError",
    "DemoScenarioRegistry",
    "DemoScenarioRegistryError",
    "DemoScenarioStep",
    "DemoSelectedAgent",
    "SocialContentDraftReadAdapter",
    "build_demo_scenario_registry",
    "build_social_content_draft_deterministic_provider",
    "build_social_content_draft_read_adapter",
    "resolve_demo_input",
]
