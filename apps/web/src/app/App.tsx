import { Link, Navigate, NavLink, Route, Routes } from "react-router-dom";

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

function AppShell(): React.JSX.Element {
  return (
    <div className="app-shell">
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
          <span>Demos</span>
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

export function App(): React.JSX.Element {
  return <AppShell />;
}
