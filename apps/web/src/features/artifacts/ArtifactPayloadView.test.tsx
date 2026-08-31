import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  AdvisoryArtifactBanner,
  ArtifactPayloadView,
  MockReceiptNotice,
} from "./ArtifactPayloadView";
import { ARTIFACT_RENDER_LIMITS } from "./artifactPayload";
import {
  ADVISORY_ARTIFACT_LABEL,
  NO_EXTERNAL_DELIVERY_LABEL,
} from "./artifactLabels";
import { safeArtifactLinkHref } from "./restrictedMarkdownSafety";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function expectNoExecutableOrEmbeddedDom(container: HTMLElement): void {
  expect(
    container.querySelector(
      "script, style, img, iframe, frame, object, embed, svg, math, video, audio, source, link",
    ),
  ).toBeNull();
  for (const element of container.querySelectorAll("*")) {
    for (const attribute of element.getAttributeNames()) {
      expect(attribute).not.toMatch(/^on/iu);
      expect(attribute).not.toBe("src");
      expect(attribute).not.toBe("srcdoc");
    }
  }
}

describe("WEB-06 ArtifactPayloadView", () => {
  it("renders redacted strings and structured JSON as inert text", () => {
    const hostile = '<img src="https://attacker.test/pixel" onerror="steal()">';
    const { container, rerender } = render(
      <ArtifactPayloadView value={hostile} label="Redacted artifact" />,
    );

    expect(
      screen.getByRole("region", { name: "Redacted artifact" }),
    ).toHaveTextContent(hostile);
    expectNoExecutableOrEmbeddedDom(container);

    rerender(
      <ArtifactPayloadView
        value={{
          title: "Safe projection",
          nested: [true, null, { hostile }],
        }}
        label="Structured artifact"
      />,
    );
    const region = screen.getByRole("region", { name: "Structured artifact" });
    expect(region).toHaveTextContent("title");
    expect(region).toHaveTextContent("Safe projection");
    expect(region).toHaveTextContent("nested");
    expect(region).toHaveTextContent(hostile);
    expectNoExecutableOrEmbeddedDom(container);
  });

  it("syntax-highlights escaped JSON with React text nodes only", () => {
    const value = JSON.parse(
      '{"markup":"</code><script>steal()</script><img src=x onerror=steal()>","__proto__":{"safe":true},"count":3}',
    );
    const { container } = render(
      <ArtifactPayloadView value={value} presentation="json" />,
    );

    const json = screen.getByLabelText("Escaped artifact JSON");
    expect(JSON.parse(json.textContent)).toEqual(value);
    expect(json).toHaveAttribute("tabindex", "0");
    expect(container.querySelectorAll(".artifact-json__key").length).toBe(4);
    expect(container.querySelector(".artifact-json__number")).toHaveTextContent(
      "3",
    );
    expectNoExecutableOrEmbeddedDom(container);
  });

  it("uses explicit depth and node caps and handles cycles and accessors without executing them", () => {
    let deep: unknown = "deep value";
    for (let depth = 0; depth <= ARTIFACT_RENDER_LIMITS.maxDepth; depth += 1) {
      deep = { next: deep };
    }
    const cycle: { self?: unknown } = {};
    cycle.self = cycle;
    const getter = vi.fn(() => "must not execute");
    const withAccessor: Record<string, unknown> = {};
    Object.defineProperty(withAccessor, "secret", {
      enumerable: true,
      get: getter,
    });

    const { rerender } = render(<ArtifactPayloadView value={deep} />);
    expect(
      screen.getByText("[Content omitted: depth limit reached]"),
    ).toBeVisible();

    rerender(<ArtifactPayloadView value={cycle} />);
    expect(
      screen.getByText("[Content omitted: circular reference]"),
    ).toBeVisible();

    rerender(<ArtifactPayloadView value={withAccessor} />);
    expect(screen.getByText("[Content omitted: accessor value]")).toBeVisible();
    expect(getter).not.toHaveBeenCalled();

    rerender(
      <ArtifactPayloadView
        value={Array.from(
          { length: ARTIFACT_RENDER_LIMITS.maxNodes + 10 },
          (_, index) => index,
        )}
      />,
    );
    expect(
      screen.getByText("[Content omitted: node limit reached]"),
    ).toBeVisible();
  });

  it("never infers Markdown from an artifact string", () => {
    const { container } = render(
      <ArtifactPayloadView value="# Heading\n[safe](https://example.test/)" />,
    );

    expect(container.querySelector("h2, h3, a")).toBeNull();
    expect(container).toHaveTextContent("# Heading");
    expect(container).toHaveTextContent("[safe](https://example.test/)");
  });

  it("renders only the restricted Markdown subset in explicit Markdown mode", () => {
    const request = vi.fn();
    vi.stubGlobal("fetch", request);
    const hostile = [
      "# Reviewed presentation",
      "<script>globalThis.__artifactPwned = true</script>",
      '<img src="https://attacker.test/pixel" onerror="steal()">',
      '<iframe src="https://attacker.test/frame"></iframe>',
      '<svg onload="steal()"><a href="javascript:steal()">x</a></svg>',
      "![remote image](https://attacker.test/image.png)",
      "[script link](javascript:steal())",
      "[mixed script](JaVaScRiPt:steal())",
      "[data link](data:text/html,<script>steal()</script>)",
      "[file link](file:///etc/passwd)",
      "[blob link](blob:https://attacker.test/id)",
      "[protocol relative](//attacker.test/path)",
      "[encoded scheme](javascript&#58;steal())",
      "[safe link](HTTPS://example.test/review?q=1)",
      "```html",
      '<img src="https://attacker.test/code.png" onerror="steal()">',
      "```",
    ].join("\n\n");

    const { container } = render(
      <ArtifactPayloadView value={hostile} presentation="markdown" />,
    );

    expect(
      screen.getByRole("heading", { name: "Reviewed presentation" }),
    ).toBeVisible();
    expect(container).toHaveTextContent("<script>globalThis.__artifactPwned");
    expect(container).toHaveTextContent("[Image omitted: remote image]");
    for (const label of [
      "script link",
      "mixed script",
      "data link",
      "file link",
      "blob link",
      "protocol relative",
      "encoded scheme",
    ]) {
      expect(
        screen.queryByRole("link", { name: label }),
      ).not.toBeInTheDocument();
    }
    const safeLink = screen.getByRole("link", { name: "safe link" });
    expect(safeLink).toHaveAttribute("href", "https://example.test/review?q=1");
    expect(safeLink).toHaveAttribute("target", "_blank");
    expect(safeLink).toHaveAttribute("rel", "noopener noreferrer");
    expect(safeLink).toHaveAttribute("referrerpolicy", "no-referrer");
    expect(
      screen.getByLabelText("Artifact Markdown code block"),
    ).toHaveAttribute("tabindex", "0");
    expectNoExecutableOrEmbeddedDom(container);
    expect(request).not.toHaveBeenCalled();
    expect(
      (globalThis as typeof globalThis & { __artifactPwned?: boolean })
        .__artifactPwned,
    ).toBeUndefined();
  });

  it("fails closed for dangerous, ambiguous, and credential-bearing URLs", () => {
    expect(safeArtifactLinkHref("https://example.test/path")).toBe(
      "https://example.test/path",
    );
    expect(safeArtifactLinkHref("http://example.test/path")).toBe(
      "http://example.test/path",
    );
    expect(safeArtifactLinkHref("javascript:alert(1)")).toBeNull();
    expect(safeArtifactLinkHref("data:text/html,attack")).toBeNull();
    expect(safeArtifactLinkHref("file:///tmp/attack")).toBeNull();
    expect(safeArtifactLinkHref("blob:https://example.test/id")).toBeNull();
    expect(safeArtifactLinkHref("//example.test/path")).toBeNull();
    expect(safeArtifactLinkHref("https://user:pass@example.test/")).toBeNull();
    expect(safeArtifactLinkHref(" https://example.test/")).toBeNull();
    expect(safeArtifactLinkHref("https://example.test/\nattack")).toBeNull();
  });

  it("renders the exact advisory and mock-delivery labels prominently", () => {
    const { container } = render(
      <>
        <AdvisoryArtifactBanner />
        <MockReceiptNotice />
      </>,
    );

    expect(screen.getByLabelText(ADVISORY_ARTIFACT_LABEL)).toHaveTextContent(
      ADVISORY_ARTIFACT_LABEL,
    );
    expect(screen.getByLabelText(NO_EXTERNAL_DELIVERY_LABEL)).toHaveTextContent(
      NO_EXTERNAL_DELIVERY_LABEL,
    );
    expect(container.querySelectorAll("strong")).toHaveLength(2);
  });
});
