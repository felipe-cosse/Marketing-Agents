import { useEffect, useRef } from "react";
import {
  Link,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";

import {
  ApprovalPendingCountBadge,
  ApprovalQueuePage,
} from "../features/approvals";
import { AgentsIcon } from "../features/org-chart/icons";
import { OrgChartPage } from "../features/org-chart/OrgChartPage";
import {
  ArtifactViewerPage,
  RunsPage,
  RunTimelinePage,
} from "../features/runs";

const SAFE_MODE_ITEMS = [
  "Local environment",
  "Deterministic mock model",
  "Mock connectors",
  "External network off",
  "Local identity — not production authentication",
] as const;

function routeLabel(pathname: string): string {
  if (pathname === "/approvals") return "Approvals";
  if (pathname === "/runs") return "Runs and audit";
  if (pathname.startsWith("/runs/")) return "Run timeline";
  if (pathname.startsWith("/artifacts/")) return "Artifact viewer";
  return "Organization chart";
}

function RouteViewport(): React.JSX.Element {
  const location = useLocation();
  const initialRenderRef = useRef(true);
  const label = routeLabel(location.pathname);

  useEffect(() => {
    document.title = `${label} | Marketing Agents`;
    if (initialRenderRef.current) {
      initialRenderRef.current = false;
      return;
    }
    requestAnimationFrame(() =>
      document.getElementById("main-content")?.focus(),
    );
  }, [label, location.pathname]);

  return (
    <div
      id="main-content"
      className="route-viewport"
      aria-label={`${label} content`}
      tabIndex={-1}
    >
      <span className="sr-only" role="status" aria-live="polite">
        {label} page loaded
      </span>
      <Routes>
        <Route path="/" element={<OrgChartPage />} />
        <Route path="/approvals" element={<ApprovalQueuePage />} />
        <Route path="/runs" element={<RunsPage />} />
        <Route path="/runs/:runId" element={<RunTimelinePage />} />
        <Route path="/artifacts/:artifactId" element={<ArtifactViewerPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}

function AppShell(): React.JSX.Element {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <header className="topbar">
        <Link className="brand-mark" to="/" aria-label="Marketing Agents home">
          <span className="brand-mark__icon">
            <AgentsIcon />
          </span>
          <span>
            <strong>Marketing Agents</strong>
            <small>Local control surface</small>
          </span>
        </Link>
        <nav className="primary-navigation" aria-label="Primary navigation">
          <NavLink
            className={({ isActive }) => (isActive ? "is-active" : undefined)}
            end
            to="/"
          >
            Org chart
          </NavLink>
          <NavLink
            className={({ isActive }) => (isActive ? "is-active" : undefined)}
            to="/approvals"
          >
            Approvals
            <ApprovalPendingCountBadge />
          </NavLink>
          <NavLink
            className={({ isActive }) => (isActive ? "is-active" : undefined)}
            to="/runs"
          >
            Runs &amp; audit
          </NavLink>
          <span>Demos unavailable</span>
        </nav>
        <div className="identity-chip">
          <span className="identity-chip__avatar" aria-hidden="true">
            LO
          </span>
          <span>
            <strong>Local operator</strong>
            <small>Not production authentication</small>
          </span>
        </div>
      </header>
      <aside className="safe-mode" aria-label="Safe local execution mode">
        <strong>
          <span aria-hidden="true">◆</span> Safe local mode
        </strong>
        <ul>
          {SAFE_MODE_ITEMS.map((item) => (
            <li key={item}>
              <span aria-hidden="true">✓</span>
              {item}
            </li>
          ))}
        </ul>
      </aside>
      <RouteViewport />
    </div>
  );
}

export function App(): React.JSX.Element {
  return <AppShell />;
}
