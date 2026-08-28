import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import { ErrorBoundary } from "./app/ErrorBoundary";
import { AppProviders } from "./app/providers";
import "./styles/tokens.css";
import "./styles/global.css";
import "./styles/org-chart.css";

const root = document.querySelector<HTMLDivElement>("#root");
if (root === null) {
  throw new Error("The Marketing Agents root element is missing.");
}

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <AppProviders>
        <App />
      </AppProviders>
    </ErrorBoundary>
  </StrictMode>,
);
