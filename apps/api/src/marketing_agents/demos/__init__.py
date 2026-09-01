"""Public deterministic-demo registry and execution contracts."""

from .blog_content_review import (
    BLOG_CONTENT_REVIEW_RENDERER,
    BLOG_CONTENT_REVIEW_SCENARIO,
    BLOG_CONTENT_REVIEW_SCENARIO_ID,
)
from .community_reminder_draft import (
    COMMUNITY_REMINDER_DRAFT_RENDERER,
    COMMUNITY_REMINDER_DRAFT_SCENARIO,
    COMMUNITY_REMINDER_DRAFT_SCENARIO_ID,
)
from .composition import (
    BlogContentReviewReadAdapter,
    CommunityReminderDraftReadAdapter,
    DeterministicDemoReadAdapter,
    EmailSignupOnboardingReadAdapter,
    SocialContentDraftReadAdapter,
    build_blog_content_review_deterministic_provider,
    build_blog_content_review_read_adapter,
    build_community_reminder_draft_deterministic_provider,
    build_community_reminder_draft_read_adapter,
    build_demo_deterministic_provider,
    build_demo_read_adapter,
    build_email_signup_onboarding_deterministic_provider,
    build_email_signup_onboarding_read_adapter,
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
from .email_signup_onboarding import (
    EMAIL_SIGNUP_ONBOARDING_RENDERER,
    EMAIL_SIGNUP_ONBOARDING_SCENARIO,
    EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID,
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
    "BLOG_CONTENT_REVIEW_RENDERER",
    "BLOG_CONTENT_REVIEW_SCENARIO",
    "BLOG_CONTENT_REVIEW_SCENARIO_ID",
    "COMMUNITY_REMINDER_DRAFT_RENDERER",
    "COMMUNITY_REMINDER_DRAFT_SCENARIO",
    "COMMUNITY_REMINDER_DRAFT_SCENARIO_ID",
    "DEMO_SCENARIOS",
    "EMAIL_SIGNUP_ONBOARDING_RENDERER",
    "EMAIL_SIGNUP_ONBOARDING_SCENARIO",
    "EMAIL_SIGNUP_ONBOARDING_SCENARIO_ID",
    "SOCIAL_CONTENT_DRAFT_RENDERER",
    "SOCIAL_CONTENT_DRAFT_SCENARIO_ID",
    "BlogContentReviewReadAdapter",
    "CommunityReminderDraftReadAdapter",
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
    "DeterministicDemoReadAdapter",
    "EmailSignupOnboardingReadAdapter",
    "SocialContentDraftReadAdapter",
    "build_blog_content_review_deterministic_provider",
    "build_blog_content_review_read_adapter",
    "build_community_reminder_draft_deterministic_provider",
    "build_community_reminder_draft_read_adapter",
    "build_demo_deterministic_provider",
    "build_demo_read_adapter",
    "build_demo_scenario_registry",
    "build_email_signup_onboarding_deterministic_provider",
    "build_email_signup_onboarding_read_adapter",
    "build_social_content_draft_deterministic_provider",
    "build_social_content_draft_read_adapter",
    "resolve_demo_input",
]
