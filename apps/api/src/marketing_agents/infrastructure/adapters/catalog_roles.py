"""OBJ-03: exact, offline role renderers; generated text never grants authority.

The common catalog input accepts narrative source_content. For calculations it
may contain a JSON object with the fields named by each report (records are
operator-supplied evidence, not observations fabricated by the mock connector).
No renderer reads a clock, opens a resource, executes a proposal, or sends mail.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from pydantic import JsonValue

from marketing_agents.application.policies.json_schema import compile_json_schema
from marketing_agents.application.ports.llm import LLMRequest
from marketing_agents.domain.canonical_json import canonical_json_bytes
from marketing_agents.domain.schema_hash import canonical_schema_hash
from marketing_agents.infrastructure.adapters.llm.deterministic import (
    DeterministicRenderContext,
    RendererKey,
    RendererRegistration,
)
from marketing_agents.infrastructure.adapters.llm.read_adapter import LLMReadBinding
from marketing_agents.infrastructure.catalog.models import CompiledCatalog
from marketing_agents.security.content_trust import ExternalContentKind

_MODEL = "cap.model.generate-structured"
_LOCAL = "cap.artifact.transform-deterministic"
_VERSION = "catalog-roles-v1"


class CatalogRoleRenderError(ValueError):
    """Payload-safe rejection of an unavailable role or malformed evidence."""


@dataclass(frozen=True, slots=True)
class _Role:
    title: str
    operation: str
    fields: tuple[str, ...] = ()
    local: bool = False


# Exact template IDs, not display-name matching or a generic fallback renderer.
_ROLES: Mapping[str, _Role] = MappingProxyType(
    {
        "tpl.social-media.new-content.linkedin-post-drafter": _Role(
            "LinkedIn post draft", "linkedin"
        ),
        "tpl.social-media.new-content.linkedin-comment-replier": _Role(
            "LinkedIn comment replies", "replies", ("comments",)
        ),
        "tpl.social-media.new-content.youtube-description-generator": _Role(
            "YouTube description and chapters", "description", ("transcript", "chapters")
        ),
        "tpl.social-media.new-content.youtube-script-generator": _Role(
            "YouTube recording script", "script", ("topic", "key_points")
        ),
        "tpl.social-media.new-content.linkedin-post-writer-new-youtube-videos": _Role(
            "LinkedIn video announcement", "video-linkedin", ("title", "key_points")
        ),
        "tpl.social-media.new-content.tweet-writer-new-youtube-videos": _Role(
            "X video announcement", "tweet", ("title",)
        ),
        "tpl.social-media.research.linkedin-lead-enricher": _Role(
            "Comment-led lead research", "leads", ("comments",)
        ),
        "tpl.social-media.research.linkedin-influencer-post-researcher": _Role(
            "Influencer post research", "influencers", ("posts",)
        ),
        "tpl.social-media.tracking-analysis.linkedin-post-tracker": _Role(
            "LinkedIn daily post report", "metrics", ("posts", "impressions", "reactions")
        ),
        "tpl.social-media.tracking-analysis.linkedin-comment-helper": _Role(
            "Comment response and lead triage", "comment-triage", ("comments",)
        ),
        "tpl.social-media.tracking-analysis.tweet-tracker": _Role(
            "X monthly post report", "metrics", ("posts", "impressions", "reposts")
        ),
        "tpl.social-media.tracking-analysis.bluesky-monitor": _Role(
            "Bluesky activity monitor", "bluesky", ("posts", "mentions", "followers")
        ),
        "tpl.blog-seo.new-content.blog-post-writer": _Role(
            "Blog editorial draft", "blog", ("title", "key_points")
        ),
        "tpl.blog-seo.new-content.blog-post-updater": _Role(
            "Blog update review", "blog-review", ("content", "required_topics")
        ),
        "tpl.blog-seo.new-content.linkedin-post-writer-new-blog-posts": _Role(
            "LinkedIn blog announcement", "blog-linkedin", ("title", "key_points")
        ),
        "tpl.blog-seo.tracking-analysis.seo-ranking-tracker": _Role(
            "Search ranking change report", "rankings", ("queries",)
        ),
        "tpl.blog-seo.tracking-analysis.feature-launch-tracker": _Role(
            "Feature launch coverage", "compare", ("expected_features", "website_features"), True
        ),
        "tpl.blog-seo.tracking-analysis.integration-tracker": _Role(
            "Integration documentation coverage",
            "compare",
            ("expected_integrations", "website_integrations"),
            True,
        ),
        "tpl.email.lifecycle-marketing.customer-onboarder": _Role(
            "Customer welcome draft", "welcome", ("customer_name", "product", "next_step")
        ),
        "tpl.email.lifecycle-marketing.new-customer-tracker": _Role(
            "New customer highlights", "customers", ("customers",)
        ),
        "tpl.email.lifecycle-marketing.churned-user-monitor": _Role(
            "Advisory customer check-in", "churn", ("customers",)
        ),
        "tpl.community.events.live-session-reminder": _Role(
            "Live session reminder draft", "reminder", ("session_title", "starts_at", "timezone")
        ),
        "tpl.community.events.event-stats-tracker": _Role(
            "Event attendance report", "attendance", ("events",)
        ),
        "tpl.community.education.material-builder": _Role(
            "Course learning material", "lesson", ("topic", "key_points")
        ),
        "tpl.community.education.course-progress-reminders": _Role(
            "Course progress and reminder drafts", "progress", ("learners",)
        ),
        "tpl.community.discussion.new-member-onboarder": _Role(
            "Community member welcome draft", "member", ("member_name", "community", "interests")
        ),
        "tpl.partnerships.implementation-partners.partner-application-reviewer": _Role(
            "Advisory partner application review", "application", ("criteria", "evidence")
        ),
        "tpl.partnerships.implementation-partners.partner-tracker": _Role(
            "Partner engagement report", "partners", ("partners",), True
        ),
        "tpl.partnerships.implementation-partners.partner-finder": _Role(
            "Supplied partner shortlist", "finder", ("requirements", "partners")
        ),
        "tpl.partnerships.implementation-partners.swag-tracker": _Role(
            "Swag fulfillment report", "swag", ("shipments",), True
        ),
        "tpl.partnerships.implementation-partners.community-challenge-tracker": _Role(
            "Community challenge points ledger", "points", ("activities",), True
        ),
        "tpl.partnerships.integration-partners.integration-partner-tracker": _Role(
            "Integration partner listing comparison",
            "compare",
            ("website_partners", "marketplace_partners"),
            True,
        ),
    }
)
_RECEIPT_ONLY = frozenset(
    {
        "tpl.email.newsletter.newsletter-subscriber",
        "tpl.email.newsletter.unsubscribe-assistant",
        "tpl.community.events.attendee-scheduler",
        "tpl.community.education.course-cohort-onboarder",
    }
)


def _clip(value: str, size: int = 600) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= size:
        return value
    return encoded[:size].decode("utf-8", errors="ignore") + "… [excerpt]"


def _text(value: Any, default: str = "not supplied") -> str:
    # Quoting single-line data prevents supplied Markdown from becoming headings.
    if type(value) is not str or not value.strip():
        return default
    return _clip(" ".join(value.split()))


def _quote(value: Any) -> str:
    return json.dumps(_text(value), ensure_ascii=False)


def _strings(data: Mapping[str, Any], field: str) -> tuple[str, ...]:
    value = data.get(field, [])
    if not isinstance(value, (list, tuple)) or len(value) > 100:
        raise CatalogRoleRenderError("role evidence list must contain at most 100 strings")
    if any(type(item) is not str or not item.strip() for item in value):
        raise CatalogRoleRenderError("role evidence list contains an invalid string")
    return tuple(value)


def _rows(data: Mapping[str, Any], field: str) -> tuple[Mapping[str, Any], ...]:
    value = data.get(field, [])
    if not isinstance(value, (list, tuple)) or len(value) > 100:
        raise CatalogRoleRenderError("role records must be a list of at most 100 objects")
    if any(not isinstance(item, Mapping) for item in value):
        raise CatalogRoleRenderError("role records contain an invalid object")
    return tuple(value)


def _number(row: Mapping[str, Any], field: str) -> float | None:
    value = row.get(field)
    if value is None:
        return None
    if type(value) not in (int, float) or not 0 <= value <= 1_000_000_000_000:
        raise CatalogRoleRenderError("role metric must be a finite number from zero through 1e12")
    return float(value)


def _source(admitted_input: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    source = cast(str, admitted_input["source_content"])
    try:
        parsed = json.loads(source)
    except (ValueError, RecursionError):
        return source, MappingProxyType({})
    return source, parsed if type(parsed) is dict else MappingProxyType({})


def _sentences(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip())[:6]


def _draft(role: _Role, source: str, data: Mapping[str, Any], audience: str) -> str:
    topic = _text(data.get("topic", data.get("title", data.get("content", source))))
    points = _strings(data, "key_points") or _sentences(topic)
    bullets = "\n".join(f"- {_quote(point)}" for point in points[:6])
    op = role.operation
    if op in {"linkedin", "video-linkedin", "blog-linkedin"}:
        hook = {
            "linkedin": "A practical idea",
            "video-linkedin": "Inside the new video",
            "blog-linkedin": "From the new article",
        }[op]
        return (
            f"{hook} for {audience}: {topic}\n\nKey takeaways:\n{bullets}"
            "\n\nWhat would you try first?"
        )
    if op == "tweet":
        # Character-bounded draft, not a fake published post or a generated URL.
        return (f"New video: {topic} — What is your biggest takeaway?")[:280]
    if op == "description":
        transcript = _text(data.get("transcript", source))
        chapters = _rows(data, "chapters")
        chapter_lines = [
            f"- {_quote(row.get('timestamp'))}: {_quote(row.get('title'))}" for row in chapters
        ]
        return (
            f"Description draft: Explore {transcript}\n\nChapters (supplied timestamps only):\n"
            + (
                "\n".join(chapter_lines)
                or "No timestamps supplied; chapters require a timed transcript."
            )
        )
    if op == "script":
        return (
            f"Opening: Today we explore {topic} for {audience}.\n\n"
            f"Talking points:\n{bullets}\n\n"
            "Demonstration: Show one example for each supplied point.\n"
            "Closing: Recap the points and ask viewers which example helped most."
        )
    if op == "blog":
        return (
            f"Working title: {topic}\n\n"
            f"Introduction: This article examines {topic} for {audience}.\n"
            f"\nSections to develop:\n{bullets}\n\n"
            "Editorial checks: Verify supplied claims, add citations, "
            "and review the conclusion before publishing. No CMS upload was performed."
        )
    if op == "lesson":
        return (
            f"Lesson: {topic}\n\nObjective: Explain the supplied concepts to {audience}.\n"
            f"\nTeaching points:\n{bullets}\n\nPractice: Apply one point to a worked example.\n"
            "Knowledge check: Explain why your example works and identify one limitation. "
            "Materials have not been shared."
        )
    if op == "welcome":
        next_step = _text(data.get("next_step"), topic)
        return (
            f"Subject: Welcome to {_text(data.get('product'))}\n\n"
            f"Hello {_text(data.get('customer_name'), 'there')}, welcome!\n"
            f"Your next step: {next_step}.\n"
            "Reply with any questions. This is an unsent draft; no CRM update is claimed."
        )
    if op == "member":
        return (
            f"Welcome {_text(data.get('member_name'), 'new member')} to "
            f"{_text(data.get('community'), 'the community')}!\n"
            f"Introduce yourself and share your interests: {_text(data.get('interests'), topic)}.\n"
            "Please review the community guidelines and ask your first question. Unsent draft."
        )
    if op == "reminder":
        return (
            f"Reminder: {_text(data.get('session_title'), topic)}\n"
            f"Starts at: {_text(data.get('starts_at'))}; timezone: {_text(data.get('timezone'))}.\n"
            "Bring your questions. Confirm the time and joining details before delivery. "
            "No reminder has been scheduled or sent."
        )
    raise CatalogRoleRenderError("role draft strategy is unavailable")


def _report(role: _Role, source: str, data: Mapping[str, Any]) -> str:
    op = role.operation
    if op == "compare":
        left, right = role.fields
        if left not in data or right not in data:
            return (
                f"Comparison unavailable: supply both {left} and {right} arrays. "
                "No absence inferred."
            )
        expected, observed = set(_strings(data, left)), set(_strings(data, right))
        return "\n".join(
            (
                f"Compared {len(expected)} unique {left} with {len(observed)} unique {right}.",
                f"Present in both: {json.dumps(sorted(expected & observed), ensure_ascii=False)}",
                f"Only in {left}: {json.dumps(sorted(expected - observed), ensure_ascii=False)}",
                f"Only in {right}: {json.dumps(sorted(observed - expected), ensure_ascii=False)}",
                "Exact supplied labels compared; no website or marketplace was fetched.",
            )
        )
    if op == "application":
        criteria = _strings(data, "criteria")
        evidence = data.get("evidence", {})
        if not isinstance(evidence, Mapping):
            raise CatalogRoleRenderError("application evidence must be an object")
        lines = [f"- {_quote(item)}: {_quote(evidence.get(item))}" for item in criteria]
        missing = sum(
            not isinstance(evidence.get(item), str) or not evidence[item].strip()
            for item in criteria
        )
        return (
            "Criterion evidence supplied for human review:\n"
            + ("\n".join(lines) or "No criteria supplied.")
            + f"\nMissing criterion evidence: {missing}. "
            "No acceptance, rejection, or score assigned."
        )
    if op == "blog-review":
        content_value = data.get("content", source)
        content = content_value if type(content_value) is str else ""
        required = _strings(data, "required_topics")
        missing_topics = [item for item in required if item.casefold() not in content.casefold()]
        return (
            f"Content word count: {len(content.split())}.\n"
            "Required topics not found by literal text match: "
            f"{json.dumps(missing_topics, ensure_ascii=False)}.\n"
            "Update recommendation: verify dates, links, citations and missing topics. "
            "Age is unknown without a supplied revision date and assessment date; "
            "no CMS edit performed."
        )
    field = "partners" if op == "finder" else role.fields[0]
    rows = _rows(data, field)
    if not rows and op in {"replies", "leads", "comment-triage"} and not data:
        rows = ({"text": source},)
    if not rows:
        return f"No {field} records supplied; no measurements or external findings inferred."
    lines = [f"Supplied {field} records: {len(rows)}."]
    if op in {"metrics", "bluesky"}:
        for metric in role.fields[1:]:
            if op == "bluesky" and metric == "followers":
                continue
            values = [_number(row, metric) for row in rows]
            present = [value for value in values if value is not None]
            lines.append(
                f"{metric}: total {sum(present):g} across "
                f"{len(present)}/{len(rows)} measured records."
            )
        lines.append(
            "Period: " + _text(data.get("period")) + "; no daily/monthly boundary inferred."
        )
        if op == "bluesky":
            before = _number(data, "previous_followers")
            after = _number(data, "current_followers")
            change = "unknown" if before is None or after is None else f"{after - before:+g}"
            lines.append(
                f"Follower change: {change}; follower snapshots are not summed across posts."
            )
    elif op == "rankings":
        for row in rows:
            before, after = _number(row, "previous_position"), _number(row, "current_position")
            change = (
                "unknown"
                if before is None or after is None
                else f"{before - after:+g} (positive = improved)"
            )
            lines.append(f"- {_quote(row.get('query'))}: position improvement {change}.")
    elif op == "attendance":
        for row in rows:
            registrations, attended = _number(row, "registrations"), _number(row, "attended")
            if registrations is not None and attended is not None and attended > registrations:
                raise CatalogRoleRenderError("attendance exceeds supplied registrations")
            rate = (
                "unknown"
                if not registrations or attended is None
                else f"{100 * attended / registrations:.1f}%"
            )
            lines.append(f"- {_quote(row.get('event'))}: attendance rate {rate}.")
    elif op == "progress":
        for row in rows:
            completed, total = _number(row, "completed"), _number(row, "total")
            if total is not None and completed is not None and completed > total:
                raise CatalogRoleRenderError("completed lessons exceed supplied total")
            progress = (
                "unknown" if not total or completed is None else f"{100 * completed / total:.1f}%"
            )
            lines.append(
                f"- {_quote(row.get('learner'))}: {progress}; unsent reminder: "
                f"Continue with {_quote(row.get('next_lesson'))} when ready."
            )
    elif op == "points":
        totals: dict[str, float] = {}
        seen: set[str] = set()
        for row in rows:
            event_id, partner = row.get("activity_id"), row.get("partner")
            points = _number(row, "points")
            if (
                type(event_id) is not str
                or not event_id
                or type(partner) is not str
                or not partner
                or points is None
            ):
                raise CatalogRoleRenderError(
                    "points require activity_id, partner and explicit points"
                )
            if event_id in seen:
                raise CatalogRoleRenderError("duplicate activity ID would double count points")
            seen.add(event_id)
            totals[partner] = totals.get(partner, 0) + points
        lines.extend(
            f"- {_quote(partner)}: {points:g} supplied points."
            for partner, points in sorted(totals.items())
        )
        lines.append("No points assigned to unspecified activities; no external ledger updated.")
    elif op == "swag":
        counts = Counter(_text(row.get("status"), "unknown") for row in rows)
        lines.extend(
            f"- Status {_quote(status)}: {count}." for status, count in sorted(counts.items())
        )
        lines.append(
            "Statuses are supplied evidence, not shipment creation or delivery confirmations."
        )
    elif op == "partners":
        for row in rows:
            interactions = _number(row, "interactions")
            count = "unknown" if interactions is None else f"{interactions:g}"
            lines.append(
                f"- {_quote(row.get('partner'))}: {count} interactions; "
                f"last contact {_quote(row.get('last_contact'))}."
            )
        lines.append(
            "Engagement is reported without inferred sentiment or fabricated contact history."
        )
    elif op == "finder":
        requirements = set(_strings(data, "requirements"))
        for row in rows:
            skills = set(_strings(row, "skills"))
            lines.append(
                f"- {_quote(row.get('partner'))}: matched "
                f"{json.dumps(sorted(skills & requirements), ensure_ascii=False)}; missing "
                f"{json.dumps(sorted(requirements - skills), ensure_ascii=False)}."
            )
        lines.append(
            "Shortlist uses supplied skills only; human validation required, no partner contacted."
        )
    elif op == "customers":
        for row in rows:
            lines.append(
                f"- {_quote(row.get('customer'))}: product {_quote(row.get('product'))}; "
                f"joined {_quote(row.get('joined_at'))}; highlight {_quote(row.get('highlight'))}."
            )
    elif op == "churn":
        for row in rows:
            lines.append(
                f"- {_quote(row.get('customer'))}: supplied signal {_quote(row.get('signal'))}; "
                "unsent check-in: Is there anything we can help you with?"
            )
        lines.append(
            "Advisory only: signals are not churn determinations; no automated account action."
        )
    elif op == "influencers":
        for row in rows:
            lines.append(
                f"- Author {_quote(row.get('author'))}, "
                f"supplied date {_quote(row.get('published_at'))}: "
                f"{_quote(row.get('text'))}."
            )
        lines.append(
            "Only supplied posts reviewed; recency and author identity not independently verified."
        )
    elif op in {"replies", "leads", "comment-triage"}:
        for row in rows:
            comment = _text(row.get("text"))
            lines.append(
                f"- Comment {_quote(comment)}; reply draft: Thanks for sharing your perspective. "
                f"Could you tell us more about {_quote(_clip(comment, 120))}?"
            )
            if op != "replies":
                lines.append(
                    f"  Supplied interest evidence: {_quote(row.get('interest'))}; "
                    "identity and purchase intent unverified; no outreach performed."
                )
    else:
        raise CatalogRoleRenderError("role report strategy is unavailable")
    return "\n".join(lines)


_DRAFTS = frozenset(
    {
        "linkedin",
        "video-linkedin",
        "blog-linkedin",
        "tweet",
        "description",
        "script",
        "blog",
        "lesson",
        "welcome",
        "member",
        "reminder",
    }
)


@dataclass(frozen=True, slots=True)
class CatalogRoleRenderer:
    """Immutable catalog-bound renderer set: 26 model roles and six local transforms."""

    catalog: CompiledCatalog

    def __post_init__(self) -> None:
        if type(self.catalog) is not CompiledCatalog:
            raise CatalogRoleRenderError("role rendering requires a compiled catalog")
        templates = {item.id: item for item in self.catalog.templates}
        if set(templates) != set(_ROLES) | _RECEIPT_ONLY:
            raise CatalogRoleRenderError(
                "catalog role inventory differs from the exact renderer registry"
            )
        for template_id, role in _ROLES.items():
            template = templates[template_id]
            model_allowed = _MODEL in template.allowed_tool_capability_ids
            if role.local:
                if (
                    model_allowed
                    or template.budget_policy.max_model_calls != 0
                    or _LOCAL not in template.allowed_tool_capability_ids
                ):
                    raise CatalogRoleRenderError("local role capabilities differ from the catalog")
            elif not model_allowed or template.budget_policy.max_model_calls < 1:
                raise CatalogRoleRenderError("model role capabilities differ from the catalog")

    @property
    def model_template_ids(self) -> tuple[str, ...]:
        return tuple(key for key, role in _ROLES.items() if not role.local)

    @property
    def local_template_ids(self) -> tuple[str, ...]:
        return tuple(key for key, role in _ROLES.items() if role.local)

    def _render(
        self,
        template_id: str,
        admitted_input: Mapping[str, Any],
        observations: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, JsonValue]:
        role = _ROLES[template_id]
        input_schema = self.catalog.input_schema_by_template[template_id]
        plain = json.loads(canonical_json_bytes(admitted_input))
        compile_json_schema(input_schema).validate(plain, pointer_root="/input", max_depth=16)
        source, data = _source(plain)
        audience = _text(plain.get("audience"), "the intended audience")
        body = (
            _draft(role, source, data, audience)
            if role.operation in _DRAFTS
            else _report(role, source, data)
        )
        header = (
            f"# {role.title}\n\nOffline deterministic artifact; supplied facts are unverified. "
            "No external delivery or write is claimed.\n"
            f"Audience: {audience}; requested locale: {_text(plain.get('locale'))} "
            "(this mock uses English templates).\n\n"
        )
        if role.operation not in _DRAFTS:
            body += f"\n\nSource context (untrusted excerpt): {_quote(_clip(source, 800))}"
        if observations:
            # Observations stay labeled evidence, never promoted to instructions.
            body += "\n\nAdditional supplied observations (unverified): " + _clip(
                canonical_json_bytes(observations).decode(), 1000
            )
        artifact = _clip(header + body, 10000)
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "template": template_id,
                    "catalog": self.catalog.content_hash,
                    "renderer_version": _VERSION,
                    "input": plain,
                    "observations": observations,
                }
            )
        ).hexdigest()[:32]
        output: dict[str, JsonValue] = {
            "artifact_id": f"artifact_{digest}",
            "summary": f"{role.title}; offline draft/report, not an external action.",
            "artifact": artifact,
            "proposed_actions": [],
            "provenance": {"template_id": template_id, "source_request_id": plain["request_id"]},
        }
        template = next(item for item in self.catalog.templates if item.id == template_id)
        if template.output_handling == "advisory":
            output["advisory"] = {
                "status": "advisory_only",
                "automated_decision": False,
                "external_action": "none",
            }
        compile_json_schema(self.catalog.output_schema_by_template[template_id]).validate(
            output, pointer_root="/output", max_depth=16
        )
        return output

    def render_local(
        self,
        template_id: str,
        admitted_input: Mapping[str, Any],
        *,
        typed_observations: Sequence[Mapping[str, Any]] = (),
        actual_receipts: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, JsonValue]:
        if template_id not in self.local_template_ids or actual_receipts:
            raise CatalogRoleRenderError(
                "local role requires an authorized transform; write finalizers are unavailable"
            )
        if len(typed_observations) > 16 or any(
            not isinstance(item, Mapping) for item in typed_observations
        ):
            raise CatalogRoleRenderError("local observations require at most 16 JSON objects")
        if len(canonical_json_bytes(typed_observations)) > 16384:
            raise CatalogRoleRenderError("local observation bytes exceed the evidence bound")
        return self._render(template_id, admitted_input, typed_observations)

    def _model_render(
        self, template_id: str, request: LLMRequest, _context: DeterministicRenderContext
    ) -> dict[str, JsonValue]:
        schema = self.catalog.output_schema_by_template[template_id]
        if (
            request.system_instructions.template_id != template_id
            or request.system_instructions.content
            != self.catalog.prompt_text_by_template[template_id].strip()
            or request.system_instructions.catalog_content_hash
            != self.catalog.content_hash.split(":", 1)[1]
            or request.output_schema_id != schema["$id"]
            or request.output_schema_hash != canonical_schema_hash(schema)
            or canonical_schema_hash(request.output_schema) != canonical_schema_hash(schema)
            or len(request.retrieved_content) != 1
            or request.tool_results
            or request.retrieved_content[0].kind is not ExternalContentKind.USER_INPUT
        ):
            raise CatalogRoleRenderError(
                "model request does not match the trusted catalog role binding"
            )
        try:
            admitted = json.loads(request.retrieved_content[0].content)
        except (ValueError, RecursionError):
            raise CatalogRoleRenderError("model role input must be a catalog JSON object") from None
        if type(admitted) is not dict:
            raise CatalogRoleRenderError("model role input must be a catalog JSON object")
        return self._render(template_id, admitted)

    def registrations(self) -> tuple[RendererRegistration, ...]:
        entries = []
        for template_id in self.model_template_ids:
            schema = self.catalog.output_schema_by_template[template_id]

            def render(
                request: LLMRequest,
                context: DeterministicRenderContext,
                selected: str = template_id,
            ) -> dict[str, JsonValue]:
                return self._model_render(selected, request, context)

            entries.append(
                RendererRegistration(
                    RendererKey(template_id, str(schema["$id"])),
                    _VERSION,
                    canonical_schema_hash(schema),
                    render,
                )
            )
        return tuple(entries)

    def model_binding(self, instance_id: str, workflow_id: str) -> LLMReadBinding:
        instance = next((item for item in self.catalog.instances if item.id == instance_id), None)
        if instance is None or instance.template_id not in self.model_template_ids:
            raise CatalogRoleRenderError("instance has no authorized model role binding")
        template_id = instance.template_id
        input_schema = self.catalog.input_schema_by_template[template_id]
        output_schema = self.catalog.output_schema_by_template[template_id]

        def transform(payload: Mapping[str, Any]) -> dict[str, Any]:
            plain: dict[str, Any] = json.loads(canonical_json_bytes(payload))
            compile_json_schema(output_schema).validate(plain, pointer_root="/output", max_depth=16)
            if plain["proposed_actions"]:
                raise CatalogRoleRenderError("role model output cannot propose dispatch authority")
            return plain

        return LLMReadBinding(
            scenario_id=workflow_id,
            template_id=template_id,
            instance_id=instance_id,
            capability_id=_MODEL,
            input_schema_id=str(input_schema["$id"]),
            input_schema=input_schema,
            model_output_schema_id=str(output_schema["$id"]),
            model_output_schema=output_schema,
            output_schema_id=str(output_schema["$id"]),
            output_schema=output_schema,
            catalog_content_hash=self.catalog.content_hash.split(":", 1)[1],
            system_prompt=self.catalog.prompt_text_by_template[template_id].strip(),
            provider_mode="mock",
            provider_name="mock",
            provider_version="v1",
            output_transform=transform,
        )
