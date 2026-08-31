import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchArtifactResource,
  RunArtifactsRequestError,
} from "../../api/runArtifacts";
import type * as RunArtifactsApi from "../../api/runArtifacts";
import {
  makeArtifactResource,
  WEB_06_ARTIFACT_ID,
  WEB_06_CATALOG_HASH,
  WEB_06_INSTANCE_ID,
  WEB_06_OUTPUT_SCHEMA_ID,
  WEB_06_PARENT_ARTIFACT_ID,
  WEB_06_PAYLOAD_DIGEST,
  WEB_06_RUN_ID,
  WEB_06_SCHEMA_HASH,
} from "../../test/runArtifactFixture";
import { ADVISORY_ARTIFACT_LABEL } from "../artifacts";
import { ArtifactViewerPage } from "./ArtifactViewerPage";

vi.mock("../../api/runArtifacts", async () => {
  const actual = await vi.importActual<typeof RunArtifactsApi>(
    "../../api/runArtifacts",
  );
  return {
    ...actual,
    fetchArtifactResource: vi.fn(),
  };
});

const fetchArtifactMock = vi.mocked(fetchArtifactResource);

function Providers({
  children,
}: {
  readonly children: ReactNode;
}): React.JSX.Element {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: Number.POSITIVE_INFINITY,
      },
    },
  });
  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

function renderArtifactPage(): ReturnType<typeof render> {
  return render(
    <Providers>
      <MemoryRouter initialEntries={[`/artifacts/${WEB_06_ARTIFACT_ID}`]}>
        <Routes>
          <Route
            path="/artifacts/:artifactId"
            element={<ArtifactViewerPage />}
          />
          <Route path="/runs/:runId" element={<p>Run page</p>} />
          <Route path="/runs" element={<p>Runs index</p>} />
        </Routes>
      </MemoryRouter>
    </Providers>,
  );
}

function fact(label: string): HTMLElement {
  const term = screen.getByText(label, { selector: "dt" });
  const row = term.parentElement;
  if (row === null) throw new Error(`Missing fact row for ${label}`);
  return row;
}

function expectNoExecutableOrEmbeddedDom(container: HTMLElement): void {
  expect(
    container.querySelector(
      "script, style, img, iframe, frame, object, embed, svg, math, video, audio, source",
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

beforeEach(() => {
  vi.resetAllMocks();
  fetchArtifactMock.mockResolvedValue(makeArtifactResource());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("WEB-06 ArtifactViewerPage", () => {
  it("shows authoritative provenance, schema, digest, provider, and advisory facts and switches to escaped JSON", async () => {
    const user = userEvent.setup();
    const artifact = makeArtifactResource();
    fetchArtifactMock.mockResolvedValue(artifact);

    renderArtifactPage();

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Artifact viewer",
      }),
    ).toBeVisible();
    expect(fact("Artifact ID")).toHaveTextContent(WEB_06_ARTIFACT_ID);
    expect(fetchArtifactMock).toHaveBeenCalledWith(
      WEB_06_ARTIFACT_ID,
      expect.any(AbortSignal),
    );
    expect(screen.getByLabelText(ADVISORY_ARTIFACT_LABEL)).toHaveTextContent(
      ADVISORY_ARTIFACT_LABEL,
    );
    expect(fact("Output schema")).toHaveTextContent(WEB_06_OUTPUT_SCHEMA_ID);
    expect(fact("Schema version")).toHaveTextContent("1.0.0");
    expect(fact("Schema hash")).toHaveTextContent(WEB_06_SCHEMA_HASH);
    expect(fact("Authorized payload digest")).toHaveTextContent(
      WEB_06_PAYLOAD_DIGEST,
    );
    expect(fact("Catalog hash")).toHaveTextContent(WEB_06_CATALOG_HASH);
    expect(fact("Sensitivity")).toHaveTextContent("Sensitive");
    expect(
      screen.getByRole("heading", {
        level: 2,
        name: "Authorized payload (bounded view)",
      }),
    ).toBeVisible();
    const producer = fact("Producer instance");
    expect(
      within(producer).getByText(WEB_06_INSTANCE_ID, { exact: true }),
    ).toBeVisible();
    expect(
      within(producer).getByText("Configuration revision 7", { exact: true }),
    ).toBeVisible();

    const sources = screen.getByRole("heading", {
      name: "Sources",
    }).parentElement;
    if (sources === null) throw new Error("Missing sources section");
    expect(sources).toHaveTextContent("Work Input");
    expect(sources).toHaveTextContent("work.web06.input.01");
    expect(sources).toHaveTextContent("Personal");
    const parents = screen.getByRole("heading", {
      name: "Parent artifacts",
    }).parentElement;
    if (parents === null) throw new Error("Missing parent artifacts section");
    expect(
      within(parents).getByRole("link", { name: WEB_06_PARENT_ARTIFACT_ID }),
    ).toHaveAttribute("href", `/artifacts/${WEB_06_PARENT_ARTIFACT_ID}`);
    const providers = screen.getByRole("heading", {
      name: "Providers",
    }).parentElement;
    if (providers === null) throw new Error("Missing providers section");
    expect(providers).toHaveTextContent("Llm");
    expect(providers).toHaveTextContent("mock-llm 2026.08");
    expect(providers).toHaveTextContent("Mock");
    expect(providers).toHaveTextContent("deterministic-planner 1.4.0");
    expect(providers).toHaveTextContent("Local");

    const structured = screen.getByRole("region", {
      name: "Authorized redacted artifact payload",
    });
    expect(structured).toHaveTextContent("recommendation");
    expect(structured).toHaveTextContent("Review the retained account signals");
    expect(
      screen.getByRole("button", { name: "Structured view" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.queryByLabelText("Escaped artifact JSON"),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Escaped JSON" }));
    expect(
      screen.getByRole("button", { name: "Escaped JSON" }),
    ).toHaveAttribute("aria-pressed", "true");
    const escaped = screen.getByLabelText("Escaped artifact JSON");
    expect(JSON.parse(escaped.textContent)).toEqual(artifact.redactedPayload);
  });

  it("keeps hostile JSON and explicit Markdown inert and permits only hardened HTTP(S) links", async () => {
    const browserRequest = vi.fn();
    vi.stubGlobal("fetch", browserRequest);
    const hostile = [
      "# Reviewed content",
      "<script>globalThis.__web06Pwned = true</script>",
      '<img src="https://attacker.test/pixel" onerror="steal()">',
      '<iframe src="https://attacker.test/frame"></iframe>',
      "![remote](https://attacker.test/image.png)",
      "[script link](javascript:steal())",
      "[data link](data:text/html,<script>steal()</script>)",
      "[file link](file:///etc/passwd)",
      "[safe review](https://example.test/review)",
    ].join("\n\n");
    fetchArtifactMock.mockResolvedValue(
      makeArtifactResource({
        redactedPayload: Object.freeze({
          artifact: hostile,
          raw_markup:
            '<object data="https://attacker.test/object"><embed src="https://attacker.test/embed"></object>',
        }),
      }),
    );
    const user = userEvent.setup();

    const { container } = renderArtifactPage();

    expect(
      await screen.findByRole("heading", { name: "Reviewed content" }),
    ).toBeVisible();
    expect(container).toHaveTextContent("<script>globalThis.__web06Pwned");
    expect(container).toHaveTextContent("[Image omitted: remote]");
    expect(
      screen.queryByRole("link", { name: "script link" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "data link" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "file link" }),
    ).not.toBeInTheDocument();
    const safeLink = screen.getByRole("link", { name: "safe review" });
    expect(safeLink).toHaveAttribute("href", "https://example.test/review");
    expect(safeLink).toHaveAttribute("target", "_blank");
    expect(safeLink).toHaveAttribute("rel", "noopener noreferrer");
    expect(safeLink).toHaveAttribute("referrerpolicy", "no-referrer");
    const markdown = container.querySelector(
      "[data-artifact-markdown='restricted']",
    );
    expect(markdown?.querySelectorAll("a")).toHaveLength(1);
    expectNoExecutableOrEmbeddedDom(container);
    expect(browserRequest).not.toHaveBeenCalled();
    expect(
      (globalThis as typeof globalThis & { __web06Pwned?: boolean })
        .__web06Pwned,
    ).toBeUndefined();

    await user.click(screen.getByRole("button", { name: "Escaped JSON" }));
    expect(screen.getByLabelText("Escaped artifact JSON")).toHaveTextContent(
      "<object data=",
    );
    expectNoExecutableOrEmbeddedDom(container);
    expect(browserRequest).not.toHaveBeenCalled();
  });

  it("shows a safe artifact error and retries only on explicit request", async () => {
    fetchArtifactMock
      .mockRejectedValueOnce(
        new RunArtifactsRequestError(
          503,
          "artifact_temporarily_unavailable",
          "The artifact is temporarily unavailable.",
        ),
      )
      .mockResolvedValueOnce(makeArtifactResource());
    const user = userEvent.setup();

    renderArtifactPage();

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The artifact is unavailable");
    expect(alert).toHaveTextContent("The artifact is temporarily unavailable.");
    expect(fetchArtifactMock).toHaveBeenCalledTimes(1);
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "Artifact viewer",
      }),
    ).toBeVisible();
    expect(fact("Artifact ID")).toHaveTextContent(WEB_06_ARTIFACT_ID);
    expect(fetchArtifactMock).toHaveBeenCalledTimes(2);
    expect(
      screen.getByRole("link", { name: `← Return to run ${WEB_06_RUN_ID}` }),
    ).toHaveAttribute("href", `/runs/${WEB_06_RUN_ID}`);
  });
});
