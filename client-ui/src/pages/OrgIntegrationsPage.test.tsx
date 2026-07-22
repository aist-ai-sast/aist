// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

vi.mock("../components/PermissionGate", () => ({
  default: ({ children }: { children: ReactNode }) => children,
}));

vi.mock("../components/ToastProvider", () => ({
  useToast: () => ({ push: vi.fn() }),
}));

let mockIntegrations: Array<{
  id: number;
  name: string;
  integration_type: string;
  is_active: boolean;
  has_secret: boolean;
  config: Record<string, string>;
  vpn_integration: number | null;
}> = [];

vi.mock("../lib/queries", () => ({
  useManageableOrgs: () => ({ data: [{ id: 1, name: "Acme" }], isLoading: false }),
  useOrgIntegrations: () => ({ data: mockIntegrations, isLoading: false, isError: false }),
  useWorkItemProviders: () => ({ data: [], isLoading: false, isError: false }),
  useProjectIntegrationOverrides: () => ({ data: [] }),
  useProjects: () => ({ data: [] }),
  useValidationStatus: () => ({ data: undefined }),
  useWorkItemProviderValidationStatus: () => ({ data: undefined }),
}));

vi.mock("../lib/mutations", () => ({
  useCreateOrgIntegration: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateOrgIntegration: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteOrgIntegration: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useValidateOrgIntegration: () => ({ mutateAsync: vi.fn().mockResolvedValue({ task_id: "t1" }), isPending: false, variables: undefined }),
  useCreateWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useValidateWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false, variables: undefined }),
  useSetProjectIntegrationOverride: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteProjectIntegrationOverride: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

import OrgIntegrationsPage from "./OrgIntegrationsPage";

describe("OrgIntegrationsPage — DAST integration", () => {
  beforeEach(() => {
    mockIntegrations = [];
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("shows Gateway URL and Integrator Token fields when DAST is selected", () => {
    render(<OrgIntegrationsPage />);

    const orgIntegrationsSection = screen.getByText("Org Integrations").closest("section")!;
    fireEvent.click(within(orgIntegrationsSection).getByRole("button", { name: /add/i }));

    const typeSelect = within(orgIntegrationsSection).getByText("Type").closest("div")!;
    fireEvent.click(within(typeSelect).getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: "DAST" }));

    expect(screen.getByText("Gateway URL")).toBeInTheDocument();
    expect(screen.getByText("Integrator Token")).toBeInTheDocument();
  });

  it("renders an existing DAST integration with the DAST badge and a working Validate action", async () => {
    mockIntegrations = [
      {
        id: 7,
        name: "Primary DAST gateway",
        integration_type: "DAST",
        is_active: true,
        has_secret: true,
        config: { gateway_url: "https://dast-gateway.internal" },
        vpn_integration: null,
      },
    ];

    render(<OrgIntegrationsPage />);

    expect(screen.getByText("DAST")).toBeInTheDocument();
    expect(screen.getByText("Primary DAST gateway")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /validate/i }));
  });
});
