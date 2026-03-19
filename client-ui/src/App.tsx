import { Navigate, Route, Routes } from "react-router-dom";
import { Suspense, lazy, useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { useQueryClient } from "@tanstack/react-query";

import Sidebar from "./components/Sidebar";
import Topbar from "./components/Topbar";
import { useAuthStatus } from "./lib/auth";
import { useToast } from "./components/ToastProvider";
import { getRoute } from "./lib/routes";
import { AUTH_EXPIRED_EVENT, isAccessDeniedError, isAuthExpiredError, toUserMessage } from "./lib/api";
import LoginPage from "./pages/LoginPage";

const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const FindingsPage = lazy(() => import("./pages/FindingsPage"));
const FindingDetailPage = lazy(() => import("./pages/FindingDetailPage"));
const ProductsPage = lazy(() => import("./pages/ProductsPage"));
const PipelinesPage = lazy(() => import("./pages/PipelinesPage"));
const CalendarPage = lazy(() => import("./pages/CalendarPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const OrgIntegrationsPage = lazy(() => import("./pages/OrgIntegrationsPage"));
const PlaceholderPage = lazy(() => import("./pages/PlaceholderPage"));

let routeBootstrapError: Error | null = null;
try {
  getRoute("ui_dashboard_path");
  getRoute("ui_findings_path");
  getRoute("ui_finding_detail_path");
  getRoute("ui_products_path");
  getRoute("ui_pipelines_path");
  getRoute("ui_calendar_path");
  getRoute("ui_settings_path");
  getRoute("ui_org_integrations_path");
} catch (error) {
  routeBootstrapError = error as Error;
}

function RequireAuth({
  children,
  forceLogin,
  onLoginSuccess,
}: {
  children: React.ReactNode;
  forceLogin: boolean;
  onLoginSuccess: () => void;
}) {
  const auth = useAuthStatus(!forceLogin);
  const toast = useToast();

  const handleLoginSuccess = () => {
    toast.push("Session active.", "success");
    onLoginSuccess();
  };

  if (!forceLogin && auth.isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-slate-300">
        Loading portal...
      </div>
    );
  }

  if (forceLogin) {
    return <LoginPage onSuccess={handleLoginSuccess} />;
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
    return <LoginPage onSuccess={handleLoginSuccess} />;
  }

  return <>{children}</>;
}

export default function App() {
  const queryClient = useQueryClient();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [forceLogin, setForceLogin] = useState(false);

  useEffect(() => {
    const handler = () => setForceLogin(true);
    window.addEventListener(AUTH_EXPIRED_EVENT, handler);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handler);
  }, []);

  if (routeBootstrapError) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-night-800 px-6 text-sm text-slate-300">
        <span>Client portal routes are not available. Ensure the server template is serving the UI.</span>
        <button
          type="button"
          className="text-brand-200 hover:underline"
          onClick={() => window.location.reload()}
        >
          Reload page
        </button>
      </div>
    );
  }

  const handleLoginSuccess = () => {
    setForceLogin(false);
    void queryClient.invalidateQueries({ queryKey: ["auth-status"] });
  };

  return (
    <div className="min-h-screen bg-night-800 text-slate-100">
      <RequireAuth forceLogin={forceLogin} onLoginSuccess={handleLoginSuccess}>
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
                  <Route path={getRoute("ui_dashboard_path")} element={<DashboardPage />} />
                  <Route path={getRoute("ui_findings_path")} element={<FindingsPage />} />
                  <Route path={getRoute("ui_finding_detail_path")} element={<FindingDetailPage />} />
                  <Route path="/finding/:id" element={<FindingDetailPage />} />
                  <Route
                    path={getRoute("ui_products_path")}
                    element={<ProductsPage />}
                  />
                  <Route
                    path={getRoute("ui_pipelines_path")}
                    element={<PipelinesPage />}
                  />
                  <Route
                    path={getRoute("ui_calendar_path")}
                    element={<CalendarPage />}
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
                  <Route
                    path={getRoute("ui_org_integrations_path")}
                    element={<OrgIntegrationsPage />}
                  />
                  <Route path="*" element={<Navigate to={getRoute("ui_dashboard_path")} replace />} />
                </Routes>
              </Suspense>
            </main>
          </div>
        </div>
      </RequireAuth>
    </div>
  );
}
