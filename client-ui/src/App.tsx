import { Navigate, Route, Routes } from "react-router-dom";
import { Suspense, lazy, useEffect, useState } from "react";
import type { CSSProperties } from "react";

import Sidebar from "./components/Sidebar";
import Topbar from "./components/Topbar";
import { useAuthStatus } from "./lib/auth";
import { useToast } from "./components/ToastProvider";
import { getRoute } from "./lib/routes";
import { AUTH_EXPIRED_EVENT, isAccessDeniedError, isAuthExpiredError, toUserMessage } from "./lib/api";
import LoginPage from "./pages/LoginPage";

const FindingsPage = lazy(() => import("./pages/FindingsPage"));
const FindingDetailPage = lazy(() => import("./pages/FindingDetailPage"));
const ProductsPage = lazy(() => import("./pages/ProductsPage"));
const PipelinesPage = lazy(() => import("./pages/PipelinesPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const PlaceholderPage = lazy(() => import("./pages/PlaceholderPage"));

let routeBootstrapError: Error | null = null;
try {
  getRoute("ui_findings_path");
  getRoute("ui_finding_detail_path");
  getRoute("ui_products_path");
  getRoute("ui_pipelines_path");
  getRoute("ui_settings_path");
} catch (error) {
  routeBootstrapError = error as Error;
}

function RequireAuth({ children, forceLogin }: { children: React.ReactNode; forceLogin: boolean }) {
  const auth = useAuthStatus(!forceLogin);
  const toast = useToast();

  if (!forceLogin && auth.isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-slate-300">
        Loading portal...
      </div>
    );
  }

  if (forceLogin) {
    return (
      <LoginPage
        onSuccess={() => {
          toast.push("Session active.", "success");
          window.location.reload();
        }}
      />
    );
  }

  if (auth.isError) {
    if (isAccessDeniedError(auth.error)) {
      return (
        <div className="rounded-2xl border border-danger-500/30 bg-night-700 p-6 text-sm text-danger-500">
          Access denied.
        </div>
      );
    }
    if (!isAuthExpiredError(auth.error)) {
      return (
        <div className="rounded-2xl border border-danger-500/30 bg-night-700 p-6 text-sm text-danger-500">
          {toUserMessage(auth.error)}
        </div>
      );
    }
    return (
      <LoginPage
        onSuccess={() => {
          toast.push("Session active.", "success");
          window.location.reload();
        }}
      />
    );
  }

  return <>{children}</>;
}

export default function App() {
  if (routeBootstrapError) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-night-800 px-6 text-sm text-slate-300">
        Client portal routes are not available. Ensure the server template is serving the UI.
      </div>
    );
  }

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [forceLogin, setForceLogin] = useState(false);

  useEffect(() => {
    const handler = () => setForceLogin(true);
    window.addEventListener(AUTH_EXPIRED_EVENT, handler);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handler);
  }, []);

  return (
    <div className="min-h-screen bg-night-800 text-slate-100">
      <RequireAuth forceLogin={forceLogin}>
        <div
          className="grid min-h-screen lg:grid-cols-[var(--sidebar-width)_1fr]"
          style={
            {
              "--sidebar-width": sidebarCollapsed ? "64px" : "208px",
            } as CSSProperties
          }
        >
          <Sidebar
            collapsed={sidebarCollapsed}
            onToggle={() => setSidebarCollapsed((value) => !value)}
          />
          <div className="flex flex-col">
            <Topbar />
            <main className="flex-1 min-h-0 px-4 py-4 pb-20 lg:px-8 lg:py-6">
              <Suspense
                fallback={(
                  <div className="rounded-2xl border border-night-500 bg-night-700 p-6 text-sm text-slate-300">
                    Loading...
                  </div>
                )}
              >
                <Routes>
                  <Route path={getRoute("ui_findings_path")} element={<FindingsPage />} />
                  <Route path={getRoute("ui_finding_detail_path")} element={<FindingDetailPage />} />
                  <Route
                    path={getRoute("ui_products_path")}
                    element={<ProductsPage />}
                  />
                  <Route
                    path={getRoute("ui_pipelines_path")}
                    element={<PipelinesPage />}
                  />
                  <Route
                    path={getRoute("ui_search_path")}
                    element={
                      <PlaceholderPage
                        title="Search"
                        description="Global search across products, findings, and pipelines will appear here."
                      />
                    }
                  />
                  <Route
                    path={getRoute("ui_settings_path")}
                    element={<SettingsPage />}
                  />
                  <Route path="*" element={<Navigate to={getRoute("ui_findings_path")} replace />} />
                </Routes>
              </Suspense>
            </main>
          </div>
        </div>
      </RequireAuth>
    </div>
  );
}
