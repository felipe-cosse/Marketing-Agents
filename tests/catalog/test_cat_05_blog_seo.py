"""CAT-05: preserve the complete source-backed Blog & SEO catalog slice."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"

EXPECTED = (
    (
        "tpl.blog-seo.new-content.blog-post-writer",
        "Blog Post Writer",
        "func.blog-seo.new-content",
        "Draft, review, and prepare new blog posts for upload.",
    ),
    (
        "tpl.blog-seo.new-content.blog-post-updater",
        "Blog Post Updater",
        "func.blog-seo.new-content",
        "Monitor blog posts and identify content that is no longer up to date.",
    ),
    (
        "tpl.blog-seo.new-content.linkedin-post-writer-new-blog-posts",
        "LinkedIn Post Writer for New Blog Posts",
        "func.blog-seo.new-content",
        "Prepare LinkedIn posts based on new blog posts.",
    ),
    (
        "tpl.blog-seo.tracking-analysis.seo-ranking-tracker",
        "SEO Ranking Tracker",
        "func.blog-seo.tracking-analysis",
        "Analyze search-query performance over time.",
    ),
    (
        "tpl.blog-seo.tracking-analysis.feature-launch-tracker",
        "Feature Launch Tracker",
        "func.blog-seo.tracking-analysis",
        "Track whether the website reflects the latest features.",
    ),
    (
        "tpl.blog-seo.tracking-analysis.integration-tracker",
        "Integration Tracker",
        "func.blog-seo.tracking-analysis",
        "Track whether the website reflects the latest integrations.",
    ),
)


def _templates() -> list[dict[str, object]]:
    return yaml.safe_load((CATALOG / "templates" / "blog-seo.yaml").read_text(encoding="utf-8"))[
        "templates"
    ]


def test_cat_05_exact_blog_seo_inventory_and_distribution() -> None:
    templates = _templates()
    assert (
        tuple(
            (item["id"], item["display_name"], item["function_id"], item["purpose"])
            for item in templates
        )
        == EXPECTED
    )
    assert Counter(item["function_id"] for item in templates) == {
        "func.blog-seo.new-content": 3,
        "func.blog-seo.tracking-analysis": 3,
    }


def test_cat_05_one_stable_instance_per_template() -> None:
    templates = _templates()
    instances = yaml.safe_load(
        (CATALOG / "instances" / "blog-seo.yaml").read_text(encoding="utf-8")
    )["instances"]
    assert [item["id"] for item in instances] == [
        "inst." + str(template["id"]).removeprefix("tpl.") + ".01" for template in templates
    ]


def test_cat_05_assets_compile_and_upload_stays_inert() -> None:
    template_schema = json.loads(
        (ROOT / "catalog" / "schema" / "template.schema.json").read_text(encoding="utf-8")
    )
    for template in _templates():
        assert list(Draft202012Validator(template_schema).iter_errors(template)) == []
        prompt = (CATALOG / str(template["system_prompt_ref"])).read_text(encoding="utf-8")
        assert "Never publish, send, enroll, unsubscribe, upload" in prompt
        for ref in (template["input_schema_ref"], template["output_schema_ref"]):
            schema = json.loads((CATALOG / str(ref)).read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
            assert schema["additionalProperties"] is False
        assert template["operation_classification"] == "read_only"
        assert not any("upload" in item for item in template["allowed_tool_capability_ids"])
        assert not any("publish" in item for item in template["allowed_tool_capability_ids"])
