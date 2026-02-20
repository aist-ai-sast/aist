import React from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";

import App from "./App";
import "./styles.css";
import "highlight.js/styles/github-dark.css";
import { ToastProvider } from "./components/ToastProvider";
import { isAccessDeniedError, isAuthExpiredError } from "./lib/api";

function shouldRetry(failureCount: number, error: unknown) {
  if (isAuthExpiredError(error) || isAccessDeniedError(error)) {
    return false;
  }
  return failureCount < 2;
}

const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error) => {
      if (isAuthExpiredError(error) || isAccessDeniedError(error)) {
        return;
      }
    },
  }),
  mutationCache: new MutationCache({
    onError: (error) => {
      if (isAuthExpiredError(error) || isAccessDeniedError(error)) {
        return;
      }
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 10 * 60 * 1000,
      gcTime: 60 * 60 * 1000,
      refetchOnWindowFocus: false,
      retry: shouldRetry,
    },
    mutations: {
      retry: shouldRetry,
    },
  },
});

const router = createBrowserRouter(
  [{ path: "*", element: <App /> }],
  { future: { v7_relativeSplatPath: true, v7_startTransition: true } },
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
