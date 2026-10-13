import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NavLink, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { ApiError } from "./api/client";
import { AuthContext, useAuth, type Auth } from "./auth/AuthContext";
import { useMe } from "./auth/me";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Callback, RequireAuth } from "./auth/RequireAuth";
import { ApprovalsInbox } from "./pages/ApprovalsInbox";
import { countLabel, usePendingApprovals } from "./pages/approvals";
import { IncidentPage } from "./pages/IncidentPage";
import { IncidentsPage } from "./pages/IncidentsPage";
import { ReleaseRiskPage, ReleaseRiskReportPage } from "./pages/ReleaseRiskPage";
import { DocumentPage, SearchPage } from "./pages/SearchPage";
import { TracePage } from "./pages/TracePage";

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      // a 4xx will not change on retry (and a 401 re-signs-in): retry only 5xx/network, twice
      queries: { retry: (n, e) => n < 2 && !(e instanceof ApiError && e.status < 500), staleTime: 10_000 },
      mutations: { retry: false }, // never re-send a decision on our own
    },
  });
}

function Layout() {
  const auth = useAuth();
  const me = useMe();
  const pending = usePendingApprovals(!!me.data);
  const n = pending.data?.length ?? 0;
  const { pathname } = useLocation();
  return (
    <div className="app">
      <nav className="top">
        <span className="brand">AEOI</span>
        <NavLink to="/incidents">Incidents</NavLink>
        <NavLink to="/approvals">
          Approvals{n > 0 && <span className="badge" aria-label={`${countLabel(n)} pending`}>{countLabel(n)}</span>}
        </NavLink>
        <NavLink to="/release-risk">Release risk</NavLink>
        <NavLink to="/search">Search</NavLink>
        <span className="spacer" />
        {me.data && (
          <span className="who small" title={me.data.permissions.join(", ")}>
            {me.data.name} · {me.data.roles.join(", ")}
          </span>
        )}
        <button className="link" onClick={() => void auth.signOut()}>Sign out</button>
      </nav>
      <main>
        <ErrorBoundary resetKey={pathname}>
          <Outlet />
        </ErrorBoundary>
      </main>
    </div>
  );
}

export function App({ auth, client }: { auth: Auth; client: QueryClient }) {
  return (
    <AuthContext.Provider value={auth}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/callback" element={<Callback />} />
          <Route element={<RequireAuth><Layout /></RequireAuth>}>
            <Route index element={<Navigate to="/incidents" replace />} />
            <Route path="/incidents" element={<IncidentsPage />} />
            <Route path="/incidents/:ref" element={<IncidentPage />} />
            <Route path="/investigations/:id" element={<TracePage />} />
            <Route path="/approvals" element={<ApprovalsInbox />} />
            <Route path="/release-risk" element={<ReleaseRiskPage />} />
            <Route path="/release-risk/:id" element={<ReleaseRiskReportPage />} />
            <Route path="/search" element={<SearchPage />} />
            <Route path="/documents/:id" element={<DocumentPage />} />
            <Route path="*" element={<p className="muted">Not found.</p>} />
          </Route>
        </Routes>
      </QueryClientProvider>
    </AuthContext.Provider>
  );
}
