"""AC-05 acceptance for the complete stable deployment-instance identity set."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from marketing_agents.infrastructure.catalog import CatalogCompilationError, compile_catalog

ROOT = Path(__file__).resolve().parents[2]
CATALOG_ROOT = ROOT / "catalog" / "v1"
TARGET_INSTANCE_ID = "inst.social-media.new-content.linkedin-post-drafter.01"

# This readable table is an independent acceptance authority copied from plan 02,
# not an expectation derived from compiler output.
EXPECTED_TEMPLATE_ORDINALS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("tpl.social-media.new-content.linkedin-post-drafter", (1,)),
    ("tpl.social-media.new-content.linkedin-comment-replier", (1,)),
    ("tpl.social-media.new-content.youtube-description-generator", (1,)),
    ("tpl.social-media.new-content.youtube-script-generator", (1,)),
    ("tpl.social-media.new-content.linkedin-post-writer-new-youtube-videos", (1,)),
    ("tpl.social-media.new-content.tweet-writer-new-youtube-videos", (1,)),
    ("tpl.social-media.research.linkedin-lead-enricher", (1,)),
    ("tpl.social-media.research.linkedin-influencer-post-researcher", (1,)),
    ("tpl.social-media.tracking-analysis.linkedin-post-tracker", (1,)),
    ("tpl.social-media.tracking-analysis.linkedin-comment-helper", (1,)),
    ("tpl.social-media.tracking-analysis.tweet-tracker", (1,)),
    ("tpl.social-media.tracking-analysis.bluesky-monitor", (1,)),
    ("tpl.blog-seo.new-content.blog-post-writer", (1,)),
    ("tpl.blog-seo.new-content.blog-post-updater", (1,)),
    ("tpl.blog-seo.new-content.linkedin-post-writer-new-blog-posts", (1,)),
    ("tpl.blog-seo.tracking-analysis.seo-ranking-tracker", (1,)),
    ("tpl.blog-seo.tracking-analysis.feature-launch-tracker", (1,)),
    ("tpl.blog-seo.tracking-analysis.integration-tracker", (1,)),
    ("tpl.email.newsletter.newsletter-subscriber", (1,)),
    ("tpl.email.newsletter.unsubscribe-assistant", (1,)),
    ("tpl.email.lifecycle-marketing.customer-onboarder", (1,)),
    ("tpl.email.lifecycle-marketing.new-customer-tracker", (1,)),
    ("tpl.email.lifecycle-marketing.churned-user-monitor", (1,)),
    ("tpl.community.events.attendee-scheduler", (1, 2)),
    ("tpl.community.events.live-session-reminder", (1, 2)),
    ("tpl.community.events.event-stats-tracker", (1, 2)),
    ("tpl.community.education.course-cohort-onboarder", (1, 2)),
    ("tpl.community.education.material-builder", (1, 2)),
    ("tpl.community.education.course-progress-reminders", (1, 2)),
    ("tpl.community.discussion.new-member-onboarder", (1, 2)),
    ("tpl.partnerships.implementation-partners.partner-application-reviewer", (1,)),
    ("tpl.partnerships.implementation-partners.partner-tracker", (1,)),
    ("tpl.partnerships.implementation-partners.partner-finder", (1,)),
    ("tpl.partnerships.implementation-partners.swag-tracker", (1,)),
    ("tpl.partnerships.implementation-partners.community-challenge-tracker", (1,)),
    ("tpl.partnerships.integration-partners.integration-partner-tracker", (1,)),
)
EXPECTED_INSTANCE_IDENTITIES = tuple(
    (
        f"inst.{template_id.removeprefix('tpl.')}.{ordinal:02d}",
        template_id,
        ordinal,
    )
    for template_id, ordinals in EXPECTED_TEMPLATE_ORDINALS
    for ordinal in ordinals
)
EXPECTED_INSTANCE_IDS = tuple(identity[0] for identity in EXPECTED_INSTANCE_IDENTITIES)


def _copy_catalog(tmp_path: Path) -> Path:
    copied_catalog = tmp_path / "catalog"
    shutil.copytree(ROOT / "catalog", copied_catalog)
    return copied_catalog / "v1"


def _social_instances(catalog_root: Path) -> tuple[Path, dict[str, Any]]:
    path = catalog_root / "instances" / "social-media.yaml"
    return path, yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_yaml(path: Path, document: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_ac_05_compiler_pins_all_43_stable_instance_identities() -> None:
    compiled = compile_catalog(CATALOG_ROOT)
    identities = tuple(
        (
            instance.id,
            instance.template_id,
            instance.variant.source_ordinal if instance.variant is not None else None,
        )
        for instance in compiled.instances
    )

    assert len(EXPECTED_TEMPLATE_ORDINALS) == 36
    assert len(EXPECTED_INSTANCE_IDENTITIES) == 43
    assert len(set(EXPECTED_INSTANCE_IDS)) == 43
    assert identities == EXPECTED_INSTANCE_IDENTITIES
    assert len({instance.id for instance in compiled.instances}) == 43


def test_ac_05_nonidentity_source_configuration_edits_preserve_ids(
    tmp_path: Path,
) -> None:
    baseline = compile_catalog(CATALOG_ROOT)
    copied_root = _copy_catalog(tmp_path)
    instance_path, document = _social_instances(copied_root)
    target = next(item for item in document["instances"] if item["id"] == TARGET_INSTANCE_ID)
    target["enabled"] = False
    target["configuration_revision"] = 2
    _write_yaml(instance_path, document)

    changed = compile_catalog(copied_root)
    baseline_ids = tuple(instance.id for instance in baseline.instances)
    changed_ids = tuple(instance.id for instance in changed.instances)
    changed_target = next(
        instance for instance in changed.instances if instance.id == TARGET_INSTANCE_ID
    )

    assert baseline_ids == changed_ids == EXPECTED_INSTANCE_IDS
    assert changed_target.enabled is False
    assert changed_target.configuration_revision == 2
    assert changed.content_hash != baseline.content_hash


@pytest.mark.parametrize(
    ("defect", "required_issue_codes"),
    (
        ("duplicate", {"duplicate-id", "duplicate-instance-id"}),
        (
            "namespace",
            {"instance-id-template-mismatch", "instance-template-identity"},
        ),
        ("ordinal", {"deployment-ordinal", "instance-source-ordinal"}),
    ),
)
def test_ac_05_production_compiler_rejects_instance_identity_drift(
    tmp_path: Path,
    defect: str,
    required_issue_codes: set[str],
) -> None:
    copied_root = _copy_catalog(tmp_path)
    instance_path, document = _social_instances(copied_root)
    target = next(item for item in document["instances"] if item["id"] == TARGET_INSTANCE_ID)

    if defect == "duplicate":
        document["instances"].append(copy.deepcopy(target))
    elif defect == "namespace":
        target["id"] = "inst.blog-seo.new-content.linkedin-post-drafter.01"
    else:
        assert defect == "ordinal"
        target["id"] = "inst.social-media.new-content.linkedin-post-drafter.02"
        target["variant"]["source_ordinal"] = 2
    _write_yaml(instance_path, document)

    with pytest.raises(CatalogCompilationError) as captured:
        compile_catalog(copied_root)

    issue_codes = {issue.code for issue in captured.value.issues}
    assert issue_codes >= required_issue_codes
