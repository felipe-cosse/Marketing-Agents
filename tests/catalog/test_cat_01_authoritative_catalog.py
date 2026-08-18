"""CAT-01: activate and verify the authoritative 5/12/36/43 catalog release."""

from __future__ import annotations

import subprocess
import sys
from collections import Counter
from pathlib import Path

from marketing_agents.infrastructure.catalog import compile_catalog

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"

EXPECTED_DEPARTMENTS = (
    "dept.social-media",
    "dept.blog-seo",
    "dept.email",
    "dept.community",
    "dept.partnerships",
)
EXPECTED_FUNCTIONS = (
    "func.social-media.new-content",
    "func.social-media.research",
    "func.social-media.tracking-analysis",
    "func.blog-seo.new-content",
    "func.blog-seo.tracking-analysis",
    "func.email.newsletter",
    "func.email.lifecycle-marketing",
    "func.community.events",
    "func.community.education",
    "func.community.discussion",
    "func.partnerships.implementation-partners",
    "func.partnerships.integration-partners",
)
EXPECTED_TEMPLATES = (
    "tpl.social-media.new-content.linkedin-post-drafter",
    "tpl.social-media.new-content.linkedin-comment-replier",
    "tpl.social-media.new-content.youtube-description-generator",
    "tpl.social-media.new-content.youtube-script-generator",
    "tpl.social-media.new-content.linkedin-post-writer-new-youtube-videos",
    "tpl.social-media.new-content.tweet-writer-new-youtube-videos",
    "tpl.social-media.research.linkedin-lead-enricher",
    "tpl.social-media.research.linkedin-influencer-post-researcher",
    "tpl.social-media.tracking-analysis.linkedin-post-tracker",
    "tpl.social-media.tracking-analysis.linkedin-comment-helper",
    "tpl.social-media.tracking-analysis.tweet-tracker",
    "tpl.social-media.tracking-analysis.bluesky-monitor",
    "tpl.blog-seo.new-content.blog-post-writer",
    "tpl.blog-seo.new-content.blog-post-updater",
    "tpl.blog-seo.new-content.linkedin-post-writer-new-blog-posts",
    "tpl.blog-seo.tracking-analysis.seo-ranking-tracker",
    "tpl.blog-seo.tracking-analysis.feature-launch-tracker",
    "tpl.blog-seo.tracking-analysis.integration-tracker",
    "tpl.email.newsletter.newsletter-subscriber",
    "tpl.email.newsletter.unsubscribe-assistant",
    "tpl.email.lifecycle-marketing.customer-onboarder",
    "tpl.email.lifecycle-marketing.new-customer-tracker",
    "tpl.email.lifecycle-marketing.churned-user-monitor",
    "tpl.community.events.attendee-scheduler",
    "tpl.community.events.live-session-reminder",
    "tpl.community.events.event-stats-tracker",
    "tpl.community.education.course-cohort-onboarder",
    "tpl.community.education.material-builder",
    "tpl.community.education.course-progress-reminders",
    "tpl.community.discussion.new-member-onboarder",
    "tpl.partnerships.implementation-partners.partner-application-reviewer",
    "tpl.partnerships.implementation-partners.partner-tracker",
    "tpl.partnerships.implementation-partners.partner-finder",
    "tpl.partnerships.implementation-partners.swag-tracker",
    "tpl.partnerships.implementation-partners.community-challenge-tracker",
    "tpl.partnerships.integration-partners.integration-partner-tracker",
)
EXPECTED_FUNCTION_TEMPLATE_COUNTS = {
    "func.social-media.new-content": 6,
    "func.social-media.research": 2,
    "func.social-media.tracking-analysis": 4,
    "func.blog-seo.new-content": 3,
    "func.blog-seo.tracking-analysis": 3,
    "func.email.newsletter": 2,
    "func.email.lifecycle-marketing": 3,
    "func.community.events": 3,
    "func.community.education": 3,
    "func.community.discussion": 1,
    "func.partnerships.implementation-partners": 5,
    "func.partnerships.integration-partners": 1,
}


def test_cat_01_exact_authoritative_inventory_compiles() -> None:
    compiled = compile_catalog(CATALOG)
    assert tuple(item.id for item in compiled.departments) == EXPECTED_DEPARTMENTS
    assert tuple(item.id for item in compiled.functions) == EXPECTED_FUNCTIONS
    assert tuple(item.id for item in compiled.templates) == EXPECTED_TEMPLATES
    assert len(compiled.instances) == 43
    assert compiled.department_instance_counts == {
        "dept.social-media": 12,
        "dept.blog-seo": 6,
        "dept.email": 5,
        "dept.community": 14,
        "dept.partnerships": 6,
    }
    assert (
        Counter(item.function_id for item in compiled.templates)
        == EXPECTED_FUNCTION_TEMPLATE_COUNTS
    )


def test_cat_01_all_43_instance_ids_are_exactly_derived() -> None:
    compiled = compile_catalog(CATALOG)
    expected = []
    for template_id in EXPECTED_TEMPLATES:
        ordinals = (1, 2) if template_id.startswith("tpl.community.") else (1,)
        expected.extend(
            f"inst.{template_id.removeprefix('tpl.')}.{ordinal:02d}" for ordinal in ordinals
        )
    assert [item.id for item in compiled.instances] == expected


def test_cat_01_prompt_schema_assets_and_hash_are_deterministic() -> None:
    first = compile_catalog(CATALOG)
    second = compile_catalog(CATALOG)
    assert first.content_hash == second.content_hash
    assert first.content_hash.startswith("catalog-sha256-v1:")
    assert len(first.prompt_text_by_template) == 36
    assert len(first.input_schema_by_template) == 36
    assert len(first.output_schema_by_template) == 36
    result = subprocess.run(
        [sys.executable, "scripts/generate_catalog_assets.py", "--root", "catalog/v1"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cat_01_capability_and_approval_policy_resolution_is_fail_closed() -> None:
    compiled = compile_catalog(CATALOG)
    capabilities = {item.id: item for item in compiled.tool_capabilities}
    policies = {item.id: item for item in compiled.approval_policies}
    assigned = {
        capability_id
        for template in compiled.templates
        for capability_id in template.allowed_tool_capability_ids
    }
    unassigned_writes = {
        item.id
        for item in capabilities.values()
        if item.effect == "write" and item.id not in assigned
    }
    assert unassigned_writes == {"cap.email.send-message", "cap.spreadsheet.update-rows"}
    for template in compiled.templates:
        if any(
            capabilities[item].effect == "write" for item in template.allowed_tool_capability_ids
        ):
            assert template.operation_classification == "mutating"
            assert policies[template.approval_policy_id].kind == "human_external_write"
