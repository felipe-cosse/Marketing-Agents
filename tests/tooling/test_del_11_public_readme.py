"""DEL-11 public README and reviewed visual assets; offline documentation controls.

Fixed hashes bind the reviewed captures, not their truth or UI behavior. These
checks execute no documented commands, browsers, services, providers, or Git.
Negative controls use small in-memory inputs and owned temporary fixtures, never
repository files. Visibility checks cover ordinary comments and code examples,
not the complete Markdown or HTML rendering semantics.
"""

from __future__ import annotations

import hashlib
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts import verify_product_docs as docs
from scripts.verify_exec_08_handoff import visible_text

ROOT = Path(__file__).resolve().parents[2]
TEXT_LIMIT = 128 * 1024
ASSET_LIMIT = 512 * 1024
SVG_LIMIT = 32 * 1024
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
CAPTURE_SOURCE = "691a03d4b1e816007bc0d12e63d29ed7c2e6cce2"
ASSETS = {
    "org-chart.png": (
        "3aaa6dd3453a66b8e5bcd498f9afbd1960f9817ff8941c07cf6f1a62f113d309",
        (1600, 1050),
    ),
    "demos.png": (
        "73dcf05c693d173c373ea4a6855601209c455ec2da3d17e33b38b14b9feb7752",
        (1600, 1050),
    ),
    "artifact-output.png": (
        "0bbe56be62b5b17af4ce1eed18b0dd8715c5b78f871c955a3349c806817a18fb",
        (1242, 920),
    ),
    "system-architecture.svg": (
        "30aeaac563cbad484900f21f7a249fc42b30affde88ecaba9b34b576abddb3fb",
        (1440, 1000),
    ),
}
IMAGE = re.compile(r"!\[([^\]\n]*)\]\(([^)\n]+)\)")
SVG_TAGS = {
    "svg",
    "title",
    "desc",
    "defs",
    "marker",
    "path",
    "filter",
    "feDropShadow",
    "style",
    "rect",
    "circle",
    "text",
    "g",
    "ellipse",
}
SVG_ATTRIBUTES = {
    "width",
    "height",
    "viewBox",
    "role",
    "aria-labelledby",
    "id",
    "markerWidth",
    "markerHeight",
    "refX",
    "refY",
    "orient",
    "markerUnits",
    "d",
    "fill",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "x",
    "y",
    "dx",
    "dy",
    "stdDeviation",
    "flood-color",
    "flood-opacity",
    "class",
    "rx",
    "ry",
    "cx",
    "cy",
    "r",
    "text-anchor",
    "filter",
    "marker-end",
    "marker-start",
}
LOCAL_URL = re.compile(r"url\(\s*#([A-Za-z_][\w.-]*)\s*\)", re.IGNORECASE)


def bounded_file(relative: str, maximum: int = TEXT_LIMIT) -> bytes:
    path = ROOT / relative
    assert path.resolve().is_relative_to(ROOT), "documentation_path_escaped"
    assert path.is_file() and not path.is_symlink(), "missing_documentation_input"
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    assert len(raw) <= maximum, "documentation_input_bound"
    return raw


def text(relative: str) -> str:
    return bounded_file(relative).decode("utf-8")


def normalized(value: str) -> str:
    return docs.plain(value).casefold()


def visible_markdown(value: str, *, retain_fences: bool = False) -> str:
    assert len(value.encode("utf-8")) <= TEXT_LIMIT, "visible_markdown_bound"
    if retain_fences:
        # Intended shell examples remain inspectable but never executable.
        return re.sub(r"<!--.*?(?:-->|\Z)", "", value, flags=re.DOTALL)
    return visible_text(value)


def assert_phrases(value: str, phrases: tuple[str, ...], code: str) -> None:
    value = normalized(value)
    for phrase in phrases:
        assert normalized(phrase) in value, code + ":" + phrase


def validate_tour(readme: str) -> None:
    examples = visible_markdown(readme, retain_fences=True)
    readme = visible_markdown(readme)
    images = IMAGE.findall(readme)
    expected = {"docs/images/" + name for name in ASSETS}
    assert len(images) == len(expected), "public_tour_image_inventory"
    assert {path for _, path in images} == expected, "public_tour_image_references"
    words = {
        "org-chart.png": ("organization", "five departments", "43"),
        "demos.png": ("social", "preset", "boundaries"),
        "artifact-output.png": ("artifact", "safety", "not-published"),
        "system-architecture.svg": ("compose", "fastapi", "postgresql"),
    }
    for alt, path in images:
        assert len(alt) >= 25, "public_tour_image_description"
        assert_phrases(alt, words[Path(path).name], "public_tour_image_description")
    anchors = docs.heading_anchors(readme)
    for anchor in (
        "quick-start",
        "product-tour",
        "architecture",
        "demo-workflows",
        "development",
        "documentation",
    ):
        assert anchor in anchors, "public_tour_anchor"
        assert "(#" + anchor + ")" in readme, "public_tour_navigation"
    assert_phrases(
        readme,
        (
            "http://127.0.0.1:8080",
            "Demos → Social content draft → Create draft",
            "Open accepted run",
            "Open artifacts",
            "5 departments",
            "12 functions",
            "36 role templates",
            "43 instances",
        ),
        "public_tour_quickstart",
    )
    assert_phrases(examples, ("make up", "make logs", "make down"), "public_tour_quickstart")
    assert "(docs/images/README.md)" in readme, "public_tour_capture_reference"
    for guide in docs.GUIDES[1:]:
        assert "(" + guide + ")" in readme, "public_tour_guide_reference"


def validate_boundaries(readme: str) -> None:
    readme = visible_markdown(readme)
    assert_phrases(
        readme,
        (
            "Model and connector responses are mocked.",
            "external delivery, and multi-user production deployment "
            "are outside the shipped runtime",
            "Screenshots show the real local UI with synthetic presets and mock providers",
            "An accepted receipt proves durable intake",
            "a mock receipt is not external delivery",
            "including self-approval",
            "does not provide production authentication",
            "diagram is not a claim of universal egress isolation",
            "does not claim a current green CI run or completed release acceptance",
            "historical results are not a fresh passing result",
            "This documentation review does not establish AC-15, AC-16, "
            "or final release acceptance",
        ),
        "public_readme_claim_boundary",
    )


def validate_capture(capture: str) -> None:
    capture = visible_markdown(capture)
    assert_phrases(
        capture,
        (
            "actual browser captures",
            "2026-10-19",
            "America/Los_Angeles",
            CAPTURE_SOURCE,
            "native supervised stack",
            "owned PostgreSQL",
            "fresh isolated state",
            "deterministic mock model",
            "mock connectors",
            "local operator identity",
            "1600 \u00d7 1050",
            "built-in Social content draft safe preset",
            "No customer data, provider credentials, or real delivery was used",
            "only the artifact panel uses an element-level crop",
            "do not establish full-suite verification or external delivery",
            "architecture diagram represents",
            "screenshots use the native development UI",
        ),
        "capture_provenance",
    )
    assert "(../../compose.yaml)" in capture, "capture_compose_reference"
    for asset in ASSETS:
        assert "(" + asset + ")" in capture, "capture_asset_reference"


def validate_native_guidance(readme: str) -> None:
    readme = visible_markdown(readme, retain_fences=True)
    assert_phrases(
        readme,
        (
            "Corepack available on PATH",
            "Python 3.12",
            "Node 24.20.0",
            "pnpm 11.24.0",
            "uv 0.10.7",
            "PostgreSQL 14+",
            "initdb",
            "postgres",
            "pg_ctl",
            "psql",
            "make bootstrap",
            "make dev",
            "http://127.0.0.1:5173",
            "make dev LOCAL_DATABASE=sqlite LOCAL_STATE=data/native",
            "retains database/key state",
            "does not acquire missing packages automatically",
        ),
        "native_development_guidance",
    )
    assert "(docs/local-operations.md#native-workflow)" in readme, "native_development_guidance"


def validate_png(raw: bytes, dimensions: tuple[int, int]) -> None:
    assert len(raw) >= 33 and raw[:8] == b"\x89PNG\r\n\x1a\n", "png_signature"
    assert raw[8:16] == b"\x00\x00\x00\rIHDR", "png_header"
    assert struct.unpack(">II", raw[16:24]) == dimensions, "png_dimensions"


def validate_svg(raw: bytes) -> None:
    assert len(raw) <= SVG_LIMIT, "svg_bound"
    assert not re.search(rb"<!\s*(?:DOCTYPE|ENTITY)|<\?", raw, re.IGNORECASE), "svg_declaration"
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise AssertionError("svg_xml_invalid") from error
    assert root.tag == "{" + SVG_NAMESPACE + "}svg", "svg_root"
    assert root.get("width") == "1440" and root.get("height") == "1000", "svg_dimensions"
    assert root.get("viewBox") == "0 0 1440 1000", "svg_viewbox"
    assert root.get("role") == "img", "svg_accessible_role"
    ids: dict[str, ET.Element] = {}
    nodes = list(root.iter())
    assert len(nodes) <= 256, "svg_node_bound"
    references = []
    for node in nodes:
        assert node.tag.startswith("{" + SVG_NAMESPACE + "}"), "svg_foreign_namespace"
        tag = node.tag.removeprefix("{" + SVG_NAMESPACE + "}")
        assert tag in SVG_TAGS, "svg_passive_elements"
        assert set(node.attrib) <= SVG_ATTRIBUTES, "svg_passive_attributes"
        if "id" in node.attrib:
            identity = node.attrib["id"]
            assert identity not in ids, "svg_duplicate_identity"
            ids[identity] = node
        values = list(node.attrib.values())
        if tag == "style":
            values.append(node.text or "")
        for value in values:
            assert len(value) <= 4096, "svg_value_bound"
            assert not re.search(
                r"@|(?:https?|file|data|javascript):|//|expression\s*\(",
                value,
                re.IGNORECASE,
            ), "svg_external_or_executable_content"
            references.extend(LOCAL_URL.findall(value))
            assert not re.search(r"url\s*\(", LOCAL_URL.sub("", value), re.IGNORECASE), (
                "svg_nonlocal_reference"
            )
    assert all(identity in ids for identity in references), "svg_missing_local_reference"
    assert root.get("aria-labelledby") == "diagram-title diagram-description", (
        "svg_accessible_labels"
    )
    for identity, tag in (("diagram-title", "title"), ("diagram-description", "desc")):
        assert identity in ids and ids[identity].tag == "{" + SVG_NAMESPACE + "}" + tag, (
            "svg_accessible_text"
        )
        assert len("".join(ids[identity].itertext()).strip()) >= 20, "svg_accessible_text"
    description = "".join(ids["diagram-description"].itertext())
    assert_phrases(
        description,
        (
            "Local Compose deployment",
            "deterministic mocks",
            "private Unix socket",
            "no IP network",
            "web proxy retains an IP network and outbound route",
            "no real providers are used",
        ),
        "svg_topology_boundary",
    )


def validate_asset(name: str, raw: bytes | None) -> None:
    assert raw is not None, "missing_visual_asset"
    assert len(raw) <= ASSET_LIMIT, "visual_asset_bound"
    digest, dimensions = ASSETS[name]
    assert hashlib.sha256(raw).hexdigest() == digest, "reviewed_asset_hash"
    if name.endswith(".png"):
        validate_png(raw, dimensions)
    else:
        validate_svg(raw)


def validate_links(relative: str, value: str) -> None:
    value = visible_markdown(value)
    for destination, fragment in docs.local_links(ROOT, relative, value):
        assert destination.exists(), "public_document_broken_link"
        if fragment:
            assert destination.suffix == ".md", "public_document_anchor_type"
            linked = text(str(destination.relative_to(ROOT)))
            assert fragment in docs.heading_anchors(linked), "public_document_broken_anchor"


@pytest.fixture(scope="module")
def readme() -> str:
    return text("README.md")


@pytest.fixture(scope="module")
def capture() -> str:
    return text("docs/images/README.md")


@pytest.fixture(scope="module")
def assets() -> dict[str, bytes]:
    return {name: bounded_file("docs/images/" + name, ASSET_LIMIT) for name in ASSETS}


def test_del11_public_tour_and_navigation(readme):
    validate_tour(readme)
    validate_links("README.md", readme)


def test_del11_mock_and_verification_claim_boundaries(readme):
    validate_boundaries(readme)


def test_del11_native_development_guidance(readme):
    validate_native_guidance(readme)


def test_del11_capture_provenance_and_local_links(capture):
    validate_capture(capture)
    validate_links("docs/images/README.md", capture)


@pytest.mark.parametrize("name", tuple(ASSETS))
def test_del11_reviewed_asset_hashes_and_dimensions(assets, name):
    validate_asset(name, assets[name])


def test_del11_passive_accessible_svg(assets):
    validate_svg(assets["system-architecture.svg"])


def test_del11_existing_product_documentation_contract():
    assert docs.verify(ROOT)["guides"] == len(docs.GUIDES)


@pytest.mark.parametrize("fault", ["missing", "reference", "external", "description"])
def test_del11_rejects_missing_or_changed_tour_images(readme, fault):
    original = next(match for match in IMAGE.finditer(readme))
    replacement = ""
    if fault == "reference":
        replacement = original.group().replace("org-chart.png", "missing.png")
    elif fault == "external":
        replacement = original.group().replace("docs/images/", "https://example.invalid/")
    elif fault == "description":
        replacement = "![](docs/images/org-chart.png)"
    changed = readme[: original.start()] + replacement + readme[original.end() :]
    with pytest.raises(AssertionError, match="public_tour_image"):
        validate_tour(changed)


@pytest.mark.parametrize("name", tuple(ASSETS))
@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_del11_rejects_missing_or_corrupt_reviewed_assets(assets, name, fault):
    raw = None if fault == "missing" else assets[name][:-1] + bytes([assets[name][-1] ^ 1])
    with pytest.raises(AssertionError, match=r"missing_visual_asset|reviewed_asset_hash"):
        validate_asset(name, raw)


@pytest.mark.parametrize(
    "phrase",
    [
        CAPTURE_SOURCE,
        "2026-10-19",
        "native supervised stack",
        "No customer data",
        "only the artifact panel uses an element-level crop",
        "native development UI",
    ],
)
def test_del11_rejects_missing_capture_provenance(capture, phrase):
    assert phrase in capture
    with pytest.raises(AssertionError, match="capture_provenance"):
        validate_capture(capture.replace(phrase, "omitted"))


@pytest.mark.parametrize(
    "phrase",
    [
        "Corepack",
        "PATH",
        "LOCAL_DATABASE=sqlite",
        "LOCAL_STATE=data/native",
        "does not acquire",
        "PostgreSQL 14+",
        "docs/local-operations.md#native-workflow",
    ],
)
def test_del11_rejects_missing_native_guidance(readme, phrase):
    assert phrase in readme
    with pytest.raises(AssertionError, match="native_development_guidance"):
        validate_native_guidance(readme.replace(phrase, "omitted"))


@pytest.mark.parametrize(
    "fragment",
    [
        "<script>alert(1)</script>",
        '<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>',
        '<image href="https://example.invalid/image.png"/>',
        '<rect onload="alert(1)"/>',
        '<style>@import "https://example.invalid/font.css";</style>',
        "<style>.card { fill: url(https://example.invalid/image); }</style>",
        "<style>.card { fill: url(data:image/png;base64,AAAA); }</style>",
        "<style>.card { fill: url(#missing); }</style>",
        "<style>.card { fill: expression(alert(1)); }</style>",
    ],
)
def test_del11_rejects_unsafe_or_unresolved_svg_content(assets, fragment):
    changed = assets["system-architecture.svg"].replace(b"</svg>", fragment.encode() + b"</svg>")
    with pytest.raises(AssertionError, match="svg_"):
        validate_svg(changed)


@pytest.mark.parametrize(
    "prefix",
    [
        b'<!DOCTYPE svg SYSTEM "https://example.invalid/external.dtd">',
        b'<!DOCTYPE svg [<!ENTITY test "unexpected">]>',
        b'<?xml-stylesheet href="https://example.invalid/style.css"?>',
    ],
)
def test_del11_rejects_svg_declarations_before_parsing(assets, prefix):
    with pytest.raises(AssertionError, match="svg_declaration"):
        validate_svg(prefix + assets["system-architecture.svg"])


@pytest.mark.parametrize("fault", ["title", "description", "role", "dimensions"])
def test_del11_rejects_svg_accessibility_or_dimensions_drift(assets, fault):
    changes = {
        "title": (b'id="diagram-title"', b'id="missing-title"'),
        "description": (b'id="diagram-description"', b'id="missing-description"'),
        "role": (b'role="img"', b'role="presentation"'),
        "dimensions": (b'width="1440"', b'width="1439"'),
    }
    old, new = changes[fault]
    assert old in assets["system-architecture.svg"]
    with pytest.raises(AssertionError, match="svg_"):
        validate_svg(assets["system-architecture.svg"].replace(old, new))


@pytest.mark.parametrize(
    "link",
    [
        "[missing](not-a-del11-document.md)",
        "[missing](../../README.md#not-a-del11-anchor)",
    ],
)
def test_del11_rejects_broken_capture_links_or_anchors(capture, link):
    with pytest.raises(AssertionError, match="public_document_broken"):
        validate_links("docs/images/README.md", capture + "\n" + link)


@pytest.mark.parametrize("fault", ["signature", "header", "dimensions", "truncated"])
def test_del11_rejects_png_header_drift_without_decompression(assets, fault):
    original = assets["org-chart.png"]
    changed = {
        "signature": b"BAD" + original[3:],
        "header": original[:12] + b"BAD!" + original[16:],
        "dimensions": original[:16] + struct.pack(">II", 1, 1) + original[24:],
        "truncated": original[:20],
    }[fault]
    with pytest.raises(AssertionError, match="png_"):
        validate_png(changed, (1600, 1050))


@pytest.mark.parametrize(
    "phrase",
    [
        "Model and connector responses are mocked.",
        "An accepted receipt proves durable intake",
        "including self-approval",
        "does not provide",
    ],
)
def test_del11_rejects_removed_mock_or_trust_boundaries(readme, phrase):
    assert phrase in readme
    with pytest.raises(AssertionError, match="public_readme_claim_boundary"):
        validate_boundaries(readme.replace(phrase, "omitted"))


def test_del11_rejects_oversize_inputs_before_parsing_or_hashing():
    with pytest.raises(AssertionError, match="svg_bound"):
        validate_svg(b"x" * (SVG_LIMIT + 1))
    with pytest.raises(AssertionError, match="visual_asset_bound"):
        validate_asset("org-chart.png", b"x" * (ASSET_LIMIT + 1))
    with pytest.raises(AssertionError, match="documentation_input_bound"):
        bounded_file("README.md", maximum=1)


@pytest.mark.parametrize("absolute", [False, True])
def test_del11_bounded_file_rejects_outside_root(tmp_path, monkeypatch, absolute):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("synthetic outside input", encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", source)
    relative = str(outside) if absolute else "../outside.md"
    with pytest.raises(AssertionError, match="documentation_path_escaped"):
        bounded_file(relative)


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_del11_bounded_file_rejects_missing_or_nonregular_input(tmp_path, monkeypatch, kind):
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    if kind == "directory":
        (tmp_path / "input.md").mkdir()
    with pytest.raises(AssertionError, match="missing_documentation_input"):
        bounded_file("input.md")


def test_del11_bounded_file_rejects_same_root_file_symlink(tmp_path, monkeypatch):
    regular = tmp_path / "regular.md"
    regular.write_text("synthetic same-root input", encoding="utf-8")
    (tmp_path / "link.md").symlink_to(regular)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    assert bounded_file("regular.md") == b"synthetic same-root input"
    with pytest.raises(AssertionError, match="missing_documentation_input"):
        bounded_file("link.md")


@pytest.mark.parametrize(
    "wrapper",
    [
        "<!--{}-->",
        "```markdown\n{}\n```",
        "~~~markdown\n{}\n~~~",
        "    {}",
    ],
)
def test_del11_hidden_tour_image_cannot_satisfy_public_reference(readme, wrapper):
    image = IMAGE.search(readme)
    assert image is not None
    changed = readme[: image.start()] + wrapper.format(image.group()) + readme[image.end() :]
    with pytest.raises(AssertionError, match="public_tour_image_inventory"):
        validate_tour(changed)


@pytest.mark.parametrize("wrapper", ["<!--{}-->", "```markdown\n{}\n```", "~~~\n{}\n~~~"])
def test_del11_hidden_navigation_cannot_satisfy_public_tour(readme, wrapper):
    navigation = "[Quick start](#quick-start)"
    assert navigation in readme
    changed = readme.replace(navigation, "\n" + wrapper.format(navigation) + "\n")
    with pytest.raises(AssertionError, match="public_tour_navigation"):
        validate_tour(changed)


@pytest.mark.parametrize(
    "wrapper",
    [
        "<!--\n{}\n-->",
        "<!--\n{}",
        "```markdown\n{}\n```",
        "~~~\n{}\n~~~",
    ],
)
def test_del11_hidden_capture_provenance_cannot_pass(capture, wrapper):
    with pytest.raises(AssertionError, match="capture_provenance"):
        validate_capture(wrapper.format(capture))


@pytest.mark.parametrize("wrapper", ["<!--{}-->", "```text\n{}\n```", "~~~\n{}\n~~~"])
def test_del11_hidden_mock_boundary_cannot_satisfy_visible_claim(readme, wrapper):
    phrase = "Model and connector responses are mocked."
    assert phrase in readme
    changed = readme.replace(phrase, "\n" + wrapper.format(phrase) + "\n")
    with pytest.raises(AssertionError, match="public_readme_claim_boundary"):
        validate_boundaries(changed)


@pytest.mark.parametrize(
    "phrase",
    [
        "Corepack available on `PATH`",
        "make dev LOCAL_DATABASE=sqlite LOCAL_STATE=data/native",
    ],
)
def test_del11_commented_native_guidance_cannot_pass(readme, phrase):
    assert phrase in readme
    with pytest.raises(AssertionError, match="native_development_guidance"):
        validate_native_guidance(readme.replace(phrase, "<!--" + phrase + "-->"))


def test_del11_native_shell_examples_remain_valid_and_visibility_is_bounded(readme):
    assert "```sh\nmake bootstrap\nmake dev\n```" in readme
    validate_native_guidance(readme)
    for retain_fences in (False, True):
        with pytest.raises(AssertionError, match="visible_markdown_bound"):
            visible_markdown("x" * (TEXT_LIMIT + 1), retain_fences=retain_fences)


def test_del11_shorter_fence_does_not_reveal_hidden_capture(capture):
    with pytest.raises(AssertionError, match="capture_provenance"):
        validate_capture("````markdown\n```\n" + capture + "\n````")
