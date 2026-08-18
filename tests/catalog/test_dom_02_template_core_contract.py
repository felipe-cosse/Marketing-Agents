"""DOM-02: every role template has exact core identity, purpose, and instructions."""

from __future__ import annotations

from pathlib import Path

from marketing_agents.infrastructure.catalog import compile_catalog
from marketing_agents.infrastructure.catalog.semantics import template_core_issues

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "catalog" / "v1"


def test_dom_02_all_36_templates_have_complete_core_fields() -> None:
    compiled = compile_catalog(CATALOG)
    assert len(compiled.templates) == 36
    assert template_core_issues(compiled.templates, compiled.prompt_text_by_template) == ()
    for template in compiled.templates:
        assert template.id
        assert template.display_name
        assert template.department_id
        assert template.function_id
        assert template.purpose
        assert compiled.prompt_text_by_template[template.id]


def test_dom_02_cross_hierarchy_template_identity_is_rejected() -> None:
    compiled = compile_catalog(CATALOG)
    original = compiled.templates[0]
    crossed = original.model_copy(update={"function_id": "func.social-media.research"})
    issues = template_core_issues(
        [crossed, *compiled.templates[1:]], compiled.prompt_text_by_template
    )
    assert "template-hierarchy-identity" in {issue.code for issue in issues}


def test_dom_02_name_purpose_and_instruction_drift_is_rejected() -> None:
    compiled = compile_catalog(CATALOG)
    original = compiled.templates[0]
    malformed = original.model_copy(
        update={"display_name": " LinkedIn Post Drafter", "purpose": "   "}
    )
    prompts = dict(compiled.prompt_text_by_template)
    prompts[original.id] = "# Different role\n\nPurpose: invented"
    codes = {
        issue.code for issue in template_core_issues([malformed, *compiled.templates[1:]], prompts)
    }
    assert codes >= {
        "template-display-name",
        "template-purpose",
        "template-instructions-identity",
    }


def test_dom_02_sibling_display_order_collision_is_rejected() -> None:
    compiled = compile_catalog(CATALOG)
    first, second = compiled.templates[:2]
    collided = second.model_copy(update={"display_order": first.display_order})
    issues = template_core_issues(
        [first, collided, *compiled.templates[2:]], compiled.prompt_text_by_template
    )
    assert "template-display-order-collision" in {issue.code for issue in issues}
