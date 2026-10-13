import { Component, type ErrorInfo, type ReactNode } from "react";

/** One broken screen must not blank the console: show what failed, keep the navigation. */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidUpdate(prev: { resetKey?: string }) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null }); // navigated away
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("render failed", error, info.componentStack);
  }
  render() {
    if (this.state.error) {
      return (
        <div className="problem" role="alert">
          <strong>This screen failed to render.</strong> {this.state.error.message}
        </div>
      );
    }
    return this.props.children;
  }
}
