import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  readonly children: ReactNode;
}

interface ErrorBoundaryState {
  readonly failed: boolean;
}

export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  override state: ErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    if (import.meta.env.DEV) {
      console.error(
        "Marketing Agents render failure",
        error,
        info.componentStack,
      );
    }
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return (
        <main className="fatal-state">
          <p className="eyebrow">Local control surface</p>
          <h1>The interface could not be rendered</h1>
          <p>
            Reload the page. If the problem persists, check the local API and
            web logs.
          </p>
          <button type="button" onClick={() => window.location.reload()}>
            Reload interface
          </button>
        </main>
      );
    }
    return this.props.children;
  }
}
