// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

let canManage = true;
vi.mock("../components/PermissionGate", () => ({
  default: ({ children, fallback = null }: { children: ReactNode; fallback?: ReactNode }) =>
    canManage ? children : fallback,
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
  dast_state?: { validation_state: string };
}> = [];
let mockProjects: Array<{ id: number; name: string; organizationId: number; productId: number }> = [];
let mockDastTargets: Array<Record<string, unknown>> = [];
let mockDastBindings: Array<Record<string, unknown>> = [];
const importDastMutateAsync = vi.fn().mockResolvedValue({ id: 7 });
const importDastReset = vi.fn();
const updateDastMutateAsync = vi.fn().mockResolvedValue({ id: 7 });
const updateDastReset = vi.fn();
const upsertBindingMutateAsync = vi.fn().mockResolvedValue({ id: 11 });
const deleteBindingMutateAsync = vi.fn().mockResolvedValue(undefined);
const createDastLaunchConfigMutateAsync = vi.fn().mockResolvedValue({ id: 31 });
const syncCapabilitiesMutateAsync = vi.fn().mockResolvedValue({ task_id: "sync-1" });

vi.mock("../lib/queries", () => ({
  useManageableOrgs: () => ({ data: [{ id: 1, name: "Acme" }], isLoading: false }),
  useOrgIntegrations: () => ({ data: mockIntegrations, isLoading: false, isError: false }),
  useWorkItemProviders: () => ({ data: [], isLoading: false, isError: false }),
  useProjectIntegrationOverrides: () => ({ data: [] }),
  useProjects: () => ({ data: mockProjects }),
  useOrganizationDastTargets: () => ({ data: mockDastTargets, isLoading: false }),
  useProjectDastBindings: () => ({ data: mockDastBindings, isLoading: false }),
  useValidationStatus: () => ({ data: undefined }),
  useWorkItemProviderValidationStatus: () => ({ data: undefined }),
}));

vi.mock("../lib/mutations", () => ({
  useCreateOrgIntegration: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateOrgIntegration: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteOrgIntegration: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useValidateOrgIntegration: () => ({ mutateAsync: vi.fn().mockResolvedValue({ task_id: "t1" }), isPending: false, variables: undefined }),
  useSyncDastCapabilities: () => ({ mutateAsync: syncCapabilitiesMutateAsync, isPending: false, variables: undefined }),
  useImportDastIntegration: () => ({ mutateAsync: importDastMutateAsync, reset: importDastReset, isPending: false }),
  useUpdateDastIntegrationOnboarding: () => ({ mutateAsync: updateDastMutateAsync, reset: updateDastReset, isPending: false }),
  useCreateWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useValidateWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false, variables: undefined }),
  useSetProjectIntegrationOverride: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteProjectIntegrationOverride: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpsertDastProjectBinding: () => ({ mutateAsync: upsertBindingMutateAsync, isPending: false }),
  useDeleteDastProjectBinding: () => ({ mutateAsync: deleteBindingMutateAsync, isPending: false }),
  useCreateDastLaunchConfig: () => ({ mutateAsync: createDastLaunchConfigMutateAsync, isPending: false }),
}));

import OrgIntegrationsPage from "./OrgIntegrationsPage";

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <OrgIntegrationsPage />
    </QueryClientProvider>,
  );
}

describe("OrgIntegrationsPage — DAST integration", () => {
  beforeEach(() => {
    mockIntegrations = [];
    mockProjects = [];
    mockDastTargets = [];
    mockDastBindings = [];
    canManage = true;
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("shows Gateway URL and Integrator Token fields when DAST is selected", () => {
    renderPage();

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

    renderPage();

    expect(screen.getByText("DAST")).toBeInTheDocument();
    expect(screen.getByText("Primary DAST gateway")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /validate/i }));
  });

  it("imports a versioned bundle and clears token-bearing UI and mutation state after submit", async () => {
    renderPage();
    const section = screen.getByText("Org Integrations").closest("section")!;
    fireEvent.click(within(section).getByRole("button", { name: /add/i }));
    const typeSelect = within(section).getByText("Type").closest("div")!;
    fireEvent.click(within(typeSelect).getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: "DAST" }));
    fireEvent.change(within(section).getByPlaceholderText("e.g. Production"), { target: { value: "Primary DAST" } });
    fireEvent.click(within(section).getAllByRole("button", { name: "Show" })[0]);
    const bundle = {
      bundle_version: 1,
      gateway_url: "https://gateway.example",
      ca_bundle: "",
      contract_major: 2,
      integrator_public_id: "pub_aist",
      server_fingerprint: "sha256:fingerprint",
      token: "pub_aist.one-time-token",
    };
    fireEvent.change(within(section).getByPlaceholderText(/bundle_version/), {
      target: { value: JSON.stringify(bundle) },
    });
    fireEvent.click(within(section).getByRole("button", { name: "Load bundle" }));
    fireEvent.click(within(section).getByRole("button", { name: "Create" }));

    await waitFor(() => expect(importDastMutateAsync).toHaveBeenCalled());
    expect(importDastMutateAsync).toHaveBeenCalledWith(expect.objectContaining({ bundle }));
    expect(importDastReset).toHaveBeenCalled();
    expect(updateDastReset).toHaveBeenCalled();
    expect(screen.queryByDisplayValue(bundle.token)).not.toBeInTheDocument();
  });

  it("renames an existing DAST integration without re-entering the integrator token", async () => {
    // The stored token is never sent back to the UI, so a rename has to go out without a bundle.
    // Sending one built from what the form holds would carry an empty token and be rejected.
    mockIntegrations = [
      {
        id: 7,
        name: "Primary DAST gateway",
        integration_type: "DAST",
        is_active: true,
        has_secret: true,
        config: {
          gateway_url: "https://gateway.example",
          ca_bundle: "",
          integrator_public_id: "pub_aist",
          server_fingerprint: "sha256:fingerprint",
        },
        vpn_integration: null,
      },
    ];

    renderPage();
    const section = screen.getByText("Org Integrations").closest("section")!;
    fireEvent.click(within(section).getByRole("button", { name: /edit/i }));
    fireEvent.change(within(section).getByPlaceholderText("e.g. Production"), {
      target: { value: "Renamed gateway" },
    });
    fireEvent.click(within(section).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateDastMutateAsync).toHaveBeenCalled());
    expect(updateDastMutateAsync).toHaveBeenCalledWith({
      integrationId: 7,
      payload: { name: "Renamed gateway", vpn_integration_id: null },
    });
  });

  it("requires the integrator token before a changed DAST connection can be saved", () => {
    // Connection fields only reach the backend inside a bundle. Without the token there is no
    // bundle, so an edit to them would be silently dropped — the control has to block instead.
    mockIntegrations = [
      {
        id: 7,
        name: "Primary DAST gateway",
        integration_type: "DAST",
        is_active: true,
        has_secret: true,
        config: {
          gateway_url: "https://gateway.example",
          ca_bundle: "",
          integrator_public_id: "pub_aist",
          server_fingerprint: "sha256:fingerprint",
        },
        vpn_integration: null,
      },
    ];

    renderPage();
    const section = screen.getByText("Org Integrations").closest("section")!;
    fireEvent.click(within(section).getByRole("button", { name: /edit/i }));
    const save = within(section).getByRole("button", { name: "Save changes" });
    expect(save).toBeEnabled();

    fireEvent.change(within(section).getByDisplayValue("https://gateway.example"), {
      target: { value: "https://moved-gateway.example:8443" },
    });

    expect(within(section).getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("hides mutation controls when manage-access permission is absent", () => {
    mockIntegrations = [
      {
        id: 7,
        name: "Primary DAST gateway",
        integration_type: "DAST",
        is_active: true,
        has_secret: true,
        config: { gateway_url: "https://gateway.example", server_fingerprint: "sha256:fingerprint" },
        vpn_integration: null,
      },
    ];
    canManage = false;

    renderPage();

    expect(screen.getByText("Primary DAST gateway")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /add|edit|delete|validate/i })).not.toBeInTheDocument();
  });
});

describe("OrgIntegrationsPage — DAST project bindings", () => {
  const target = {
    id: 5,
    provider_id: "web-app",
    display_name: "Web application",
    contract_revision: "2.0",
    capability_revision: "cap-7",
    schema_digest: "schema-7",
    parameter_schema: {
      type: "object",
      additionalProperties: false,
      required: ["mode", "label", "count", "advanced"],
      properties: {
        mode: { type: "string", title: "Mode", enum: ["fast", "deep"] },
        label: { type: "string", title: "Label" },
        count: { type: "number", title: "Count", minimum: 1 },
        advanced: { type: "boolean", title: "Advanced" },
      },
      if: { properties: { advanced: { const: true } } },
      then: {
        required: ["details"],
        properties: {
          details: {
            type: "object",
            title: "Details",
            additionalProperties: false,
            required: ["note"],
            properties: { note: { type: "string", title: "Note" } },
          },
        },
      },
    },
    provider_defaults: { mode: "fast", label: "baseline", count: 2, advanced: false },
    repository_keys: ["source"],
    autonomous_ready: true,
    is_available: true,
    last_seen_at: "2026-07-25T00:00:00Z",
  };

  beforeEach(() => {
    canManage = true;
    mockProjects = [{ id: 9, name: "Checkout", organizationId: 1, productId: 3 }];
    mockDastTargets = [target];
    mockDastBindings = [];
    vi.clearAllMocks();
  });

  afterEach(() => cleanup());

  function selectProject() {
    const section = screen.getByText("DAST Target Bindings").closest("section")!;
    fireEvent.click(within(section).getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: "Checkout" }));
    return section;
  }

  it("submits the complete provider-defaulted schema object", async () => {
    renderPage();
    const section = selectProject();
    fireEvent.click(within(section).getByRole("button", { name: /add/i }));

    expect(within(section).getByText("Mode")).toBeInTheDocument();
    expect(within(section).getByText("Label")).toBeInTheDocument();
    expect(within(section).getByText("Count")).toBeInTheDocument();
    fireEvent.click(within(section).getByRole("button", { name: "Save binding" }));

    await waitFor(() => expect(upsertBindingMutateAsync).toHaveBeenCalled());
    expect(upsertBindingMutateAsync).toHaveBeenCalledWith({
      target_id: 5,
      capability_revision: "cap-7",
      schema_digest: "schema-7",
      source_repo_key: "source",
      enabled: true,
      parameter_snapshot: { mode: "fast", label: "baseline", count: 2, advanced: false },
      autonomous_enabled: false,
    });
  });

  it("hides the DAST Target Bindings section entirely (not just its buttons) without operate permission", () => {
    canManage = false;
    mockDastBindings = [
      {
        id: 11,
        project: 9,
        target,
        source_repo_key: "source",
        enabled: true,
        parameter_snapshot: {},
        autonomous_enabled: false,
        readiness: { ready: true, issues: [], checked_at: "2026-07-25T00:00:00Z" },
      },
    ];
    renderPage();
    const section = screen.getByText("DAST Target Bindings").closest("section")!;

    expect(within(section).getByText("Binding management is available to the organization administrator.")).toBeInTheDocument();
    expect(within(section).queryByRole("combobox")).not.toBeInTheDocument();
    expect(within(section).queryByText("Web application")).not.toBeInTheDocument();
  });

  it("creates a DAST launch config from an enabled binding", async () => {
    mockDastBindings = [
      {
        id: 11,
        project: 9,
        target,
        source_repo_key: "source",
        enabled: true,
        parameter_snapshot: { mode: "fast", label: "baseline", count: 2, advanced: false },
        autonomous_enabled: true,
        readiness: { ready: true, issues: [], checked_at: "2026-07-25T00:00:00Z" },
      },
    ];
    renderPage();
    const section = selectProject();

    fireEvent.click(within(section).getByRole("button", { name: "Create launch config" }));
    fireEvent.change(within(section).getByLabelText("Launch config name"), {
      target: { value: "Nightly web DAST" },
    });
    fireEvent.click(within(section).getByRole("button", { name: "Save launch config" }));

    await waitFor(() => expect(createDastLaunchConfigMutateAsync).toHaveBeenCalledWith({
      bindingId: 11,
      name: "Nightly web DAST",
      params: { mode: "fast", label: "baseline", count: 2, advanced: false },
    }));
  });
  it("offers a catalog refresh only for a validated DAST integration", async () => {
    mockIntegrations = [
      {
        id: 7,
        name: "Primary DAST gateway",
        integration_type: "DAST",
        is_active: true,
        has_secret: true,
        config: { gateway_url: "https://gateway.example" },
        vpn_integration: null,
        dast_state: { validation_state: "READY" },
      },
    ];

    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /Synchronize/ }));

    await waitFor(() => expect(syncCapabilitiesMutateAsync).toHaveBeenCalledWith(7));
  });

  it("hides the catalog refresh until the integration has validated", () => {
    mockIntegrations = [
      {
        id: 7,
        name: "Primary DAST gateway",
        integration_type: "DAST",
        is_active: true,
        has_secret: true,
        config: { gateway_url: "https://gateway.example" },
        vpn_integration: null,
        dast_state: { validation_state: "PENDING_VALIDATION" },
      },
    ];

    renderPage();

    expect(screen.queryByRole("button", { name: /Synchronize/ })).not.toBeInTheDocument();
  });

  it("never offers a catalog refresh for a non-DAST integration", () => {
    mockIntegrations = [
      {
        id: 3,
        name: "GitLab",
        integration_type: "GITLAB",
        is_active: true,
        has_secret: true,
        config: {},
        vpn_integration: null,
      },
    ];

    renderPage();

    expect(screen.queryByRole("button", { name: /Synchronize/ })).not.toBeInTheDocument();
  });
});
