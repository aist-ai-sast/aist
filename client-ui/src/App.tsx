import { Route, Routes } from "react-router-dom";

import Sidebar from "./components/Sidebar";
import Topbar from "./components/Topbar";
import FindingsPage from "./pages/FindingsPage";
import FindingDetailPage from "./pages/FindingDetailPage";
import PlaceholderPage from "./pages/PlaceholderPage";
import { logoutSession, useAuthStatus } from "./lib/auth";
import LoginPage from "./pages/LoginPage";
import { useToast } from "./components/ToastProvider";
import { getRoute } from "./lib/routes";
import ProductsPage from "./pages/ProductsPage";

function RequireAuth({ children }: { children: React.ReactNode }) {
  const auth = useAuthStatus();
  const toast = useToast();

  if (auth.isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-slate-300">
        Loading portal...
      </div>
    );
  }

  if (auth.isError) {
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
  try {
    getRoute("login_url");
  } catch {
    return (
      <div className="flex min-h-screen items-center justify-center bg-night-800 px-6 text-sm text-slate-300">
        Client portal routes are not available. Ensure the server template is serving the UI.
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-night-800 text-slate-100">
      <RequireAuth>
        <div className="grid min-h-screen lg:grid-cols-[256px_1fr]">
          <Sidebar />
          <div className="flex flex-col">
            <Topbar />
            <main className="flex-1 min-h-0 px-6 py-6 lg:px-8">
              <Routes>
                <Route path="/" element={<FindingsPage />} />
                <Route path="/finding/:id" element={<FindingDetailPage />} />
                <Route
                  path="/products"
                  element={<ProductsPage />}
                />
                <Route
                  path="/pipelines"
                  element={
                    <PlaceholderPage
                      title="Pipelines"
                      description="Pipeline history, AI results, and actions timeline will be available here."
                    />
                  }
                />
                <Route
                  path="/search"
                  element={
                    <PlaceholderPage
                      title="Search"
                      description="Global search across products, findings, and pipelines will appear here."
                    />
                  }
                />
                <Route
                  path="/settings"
                  element={
                    <PlaceholderPage
                      title="Settings"
                      description="Profile, notifications, and API token settings will appear here."
                    />
                  }
                />
              </Routes>
            </main>
          </div>
        </div>
      </RequireAuth>
    </div>
  );
}
