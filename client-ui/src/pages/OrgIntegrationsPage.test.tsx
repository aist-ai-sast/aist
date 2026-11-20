// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { ReactNode } from "react";

import type { BuildEnvVariableStatus, ProjectIntegrationOverride } from "../lib/queries";
import type { Project } from "../types";

let canManage = true;
vi.mock("../components/PermissionGate", () => ({
  default: ({ children, fallback = null }: { children: ReactNode; fallback?: ReactNode }) =>
    canManage ? children : fallback,
}));

const toastPush = vi.fn();
vi.mock("../components/ToastProvider", () => ({
  useToast: () => ({ push: toastPush }),
}));

let mockIntegrations: Array<{
  id: number;
  name: string;
  integration_type: string;
  is_active: boolean;
  has_secret: boolean;
  config: Record<string, string>;
  vpn_integration: number | null;
  build_env?: BuildEnvVariableStatus[];
  dast_state?: { validation_state: string; sync_status?: string; sync_error_code?: string };
}> = [];
let mockValidationStatus: { state: string; valid: boolean | null; detail: string } | undefined;
let mockProjects: Project[] = [];
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
const disableDastMutateAsync = vi.fn().mockResolvedValue({ id: 7, is_active: false });
const deleteIntegrationMutateAsync = vi.fn().mockResolvedValue(undefined);
const createIntegrationMutateAsync = vi.fn().mockResolvedValue({ id: 21 });
const updateIntegrationMutateAsync = vi.fn().mockResolvedValue({ id: 21 });
const setOverrideMutateAsync = vi.fn().mockResolvedValue({});
const deleteOverrideMutateAsync = vi.fn().mockResolvedValue(undefined);
let mockOverrides: ProjectIntegrationOverride[] = [];

vi.mock("../lib/queries", async () => ({
  isDastSyncInFlight: (await vi.importActual<typeof import("../lib/queries")>("../lib/queries")).isDastSyncInFlight,
  useManageableOrgs: () => ({ data: [{ id: 1, name: "Acme" }], isLoading: false }),
  useOrgIntegrations: () => ({ data: mockIntegrations, isLoading: false, isError: false }),
  useWorkItemProviders: () => ({ data: [], isLoading: false, isError: false }),
  useProjectIntegrationOverrides: () => ({ data: mockOverrides }),
  useProjects: () => ({ data: mockProjects }),
  useOrganizationDastTargets: () => ({ data: mockDastTargets, isLoading: false }),
  useProjectDastBindings: () => ({ data: mockDastBindings, isLoading: false }),
  useValidationStatus: (_id: number | null, taskId: string | null) => ({
    data: taskId ? mockValidationStatus : undefined,
  }),
  useWorkItemProviderValidationStatus: () => ({ data: undefined }),
}));

vi.mock("../lib/mutations", async () => ({
  invalidateDastLifecycleQueries: (
    await vi.importActual<typeof import("../lib/mutations")>("../lib/mutations")
  ).invalidateDastLifecycleQueries,
  useCreateOrgIntegration: () => ({ mutateAsync: createIntegrationMutateAsync, isPending: false }),
  useUpdateOrgIntegration: () => ({ mutateAsync: updateIntegrationMutateAsync, isPending: false }),
  useDeleteOrgIntegration: () => ({ mutateAsync: deleteIntegrationMutateAsync, isPending: false }),
  useDisableDastIntegration: () => ({ mutateAsync: disableDastMutateAsync, isPending: false }),
  useValidateOrgIntegration: () => ({ mutateAsync: vi.fn().mockResolvedValue({ task_id: "t1" }), isPending: false, variables: undefined }),
  useSyncDastCapabilities: () => ({ mutateAsync: syncCapabilitiesMutateAsync, isPending: false, variables: undefined }),
  useImportDastIntegration: () => ({ mutateAsync: importDastMutateAsync, reset: importDastReset, isPending: false }),
  useUpdateDastIntegrationOnboarding: () => ({ mutateAsync: updateDastMutateAsync, reset: updateDastReset, isPending: false }),
  useCreateWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useUpdateWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useDeleteWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useValidateWorkItemProvider: () => ({ mutateAsync: vi.fn(), isPending: false, variables: undefined }),
  useSetProjectIntegrationOverride: () => ({ mutateAsync: setOverrideMutateAsync, isPending: false }),
  useDeleteProjectIntegrationOverride: () => ({ mutateAsync: deleteOverrideMutateAsync, isPending: false }),
  useUpsertDastProjectBinding: () => ({ mutateAsync: upsertBindingMutateAsync, isPending: false }),
  useDeleteDastProjectBinding: () => ({ mutateAsync: deleteBindingMutateAsync, isPending: false }),
  useCreateDastLaunchConfig: () => ({ mutateAsync: createDastLaunchConfigMutateAsync, isPending: false }),
}));

import OrgIntegrationsPage from "./OrgIntegrationsPage";

function renderPage(
  queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  }),
) {
  const tree = () => (
    <QueryClientProvider client={queryClient}>
      <OrgIntegrationsPage />
    </QueryClientProvider>
  );
  const view = render(tree());
  return { ...view, rerenderPage: () => view.rerender(tree()) };
}

function orgSection() {
  return screen.getByText("Org Integrations").closest("section")!;
}

/** Open a new integration form in the Org Integrations section, pick ``typeLabel`` and type ``name``. */
function startNewIntegration(typeLabel: string, name?: string) {
  const section = orgSection();
  fireEvent.click(within(section).getByRole("button", { name: /add/i }));
  const typeSelect = within(section).getByText("Type").closest("div")!;
  fireEvent.click(within(typeSelect).getByRole("combobox"));
  fireEvent.click(screen.getByRole("option", { name: typeLabel }));
  if (name !== undefined) {
    fireEvent.change(within(section).getByPlaceholderText("e.g. Production"), { target: { value: name } });
  }
  return section;
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

    startNewIntegration("DAST");

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

  it("disables an active DAST integration without offering destructive delete", async () => {
    mockIntegrations = [{
      id: 7,
      name: "Primary DAST gateway",
      integration_type: "DAST",
      is_active: true,
      has_secret: true,
      config: { gateway_url: "https://gateway.example" },
      vpn_integration: null,
    }];
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderPage();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));

    await waitFor(() => expect(disableDastMutateAsync).toHaveBeenCalledWith(7));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("history"));
    expect(deleteIntegrationMutateAsync).not.toHaveBeenCalled();
  });

  it("deletes only an inactive DAST integration with an explicit dependency warning", async () => {
    mockIntegrations = [{
      id: 7,
      name: "Retired DAST gateway",
      integration_type: "DAST",
      is_active: false,
      has_secret: true,
      config: { gateway_url: "https://gateway.example" },
      vpn_integration: null,
    }];
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteIntegrationMutateAsync).toHaveBeenCalledWith(7));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Targets, bindings, presets, schedules"));
    expect(disableDastMutateAsync).not.toHaveBeenCalled();
  });

  it("does not render the misleading Active checkbox while editing DAST", () => {
    mockIntegrations = [{
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
    }];

    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /edit/i }));

    expect(screen.queryByLabelText("Active")).not.toBeInTheDocument();
  });

  it("imports a versioned bundle and clears token-bearing UI and mutation state after submit", async () => {
    renderPage();
    const section = startNewIntegration("DAST", "Primary DAST");
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
    const section = orgSection();
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
    const section = orgSection();
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
    launch_requirements: ["repository-trigger"],
    autonomous_ready: true,
    is_available: true,
    last_seen_at: "2026-07-25T00:00:00Z",
  };

  // The second shape of target: the provider scans a running system and declares no repository.
  const perimeterTarget = {
    ...target,
    id: 6,
    provider_id: "perimeter",
    display_name: "External perimeter",
    repository_keys: [],
    launch_requirements: [],
  };

  beforeEach(() => {
    canManage = true;
    mockIntegrations = [
      {
        id: 7,
        name: "Primary DAST gateway",
        integration_type: "DAST",
        is_active: true,
        has_secret: true,
        config: { gateway_url: "https://gateway.example" },
        vpn_integration: null,
      },
    ];
    mockProjects = [{ id: 9, name: "Checkout", organizationId: 1, productId: 3, canOperate: true }];
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

  it("shows target bindings only after the organization has a DAST integration", () => {
    mockIntegrations = [];
    const { rerender } = renderPage();

    expect(screen.queryByText("DAST Target Bindings")).not.toBeInTheDocument();

    mockIntegrations = [
      {
        id: 7,
        name: "Retired DAST gateway",
        integration_type: "DAST",
        is_active: false,
        has_secret: true,
        config: { gateway_url: "https://gateway.example" },
        vpn_integration: null,
      },
    ];
    rerender(
      <QueryClientProvider client={new QueryClient()}>
        <OrgIntegrationsPage />
      </QueryClientProvider>,
    );

    expect(screen.getByText("DAST Target Bindings")).toBeInTheDocument();
  });

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
        readiness: { ready: true, issues: [], checked_at: "2026-07-25T00:00:00Z" },
      },
    ];
    renderPage();
    const section = screen.getByText("DAST Target Bindings").closest("section")!;

    expect(within(section).getByText("Binding management is available to the organization administrator.")).toBeInTheDocument();
    expect(within(section).queryByRole("combobox")).not.toBeInTheDocument();
    expect(within(section).queryByText("Web application")).not.toBeInTheDocument();
  });

  it("does not offer launch-config creation — that lives in the AIST admin launch dashboard", () => {
    mockDastBindings = [
      {
        id: 11,
        project: 9,
        target,
        source_repo_key: "source",
        enabled: true,
        parameter_snapshot: { mode: "fast", label: "baseline", count: 2, advanced: false },
        readiness: { ready: true, issues: [], checked_at: "2026-07-25T00:00:00Z" },
      },
    ];
    renderPage();
    const section = selectProject();

    // The old control created a config client-ui could not then list, edit, start or schedule.
    expect(within(section).queryByRole("button", { name: "Create launch config" })).not.toBeInTheDocument();
    expect(within(section).queryByLabelText("Launch config name")).not.toBeInTheDocument();
    expect(createDastLaunchConfigMutateAsync).not.toHaveBeenCalled();
    // Binding management itself stays here.
    expect(within(section).getByRole("button", { name: "Edit" })).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Remove" })).toBeInTheDocument();
  });

  it("summarises a binding by its source repository without leaking provider digests", () => {
    mockDastBindings = [
      {
        id: 11,
        project: 9,
        target,
        source_repo_key: "source",
        enabled: true,
        parameter_snapshot: {},
        readiness: { ready: true, issues: [], checked_at: "2026-07-25T00:00:00Z" },
      },
    ];
    renderPage();
    const section = selectProject();

    expect(within(section).getByText(/Source repository: source/)).toBeInTheDocument();
    // schema_digest / capability_revision are launch-admission machinery the operator
    // cannot act on; the backend fails the launch when they drift.
    expect(section.textContent).not.toContain("schema-7");
    expect(section.textContent).not.toContain("cap-7");
  });

  it("summarises a perimeter binding without an empty source-repository line", () => {
    mockDastBindings = [
      {
        id: 12,
        project: 9,
        target: perimeterTarget,
        source_repo_key: "",
        enabled: true,
        parameter_snapshot: {},
        readiness: { ready: true, issues: [], checked_at: "2026-07-25T00:00:00Z" },
      },
    ];
    renderPage();
    const section = selectProject();

    expect(within(section).getByText("External perimeter")).toBeInTheDocument();
    expect(within(section).queryByText(/Source repository/)).not.toBeInTheDocument();
    // Readiness means "this binding can launch" — nothing about launching without an operator.
    expect(within(section).getByText("Ready to launch")).toBeInTheDocument();
  });

  it("offers no source-repository selector when editing a perimeter binding", () => {
    mockDastTargets = [perimeterTarget];
    mockDastBindings = [
      {
        id: 12,
        project: 9,
        target: perimeterTarget,
        source_repo_key: "",
        enabled: true,
        parameter_snapshot: { mode: "fast", label: "baseline", count: 2, advanced: false },
        readiness: { ready: true, issues: [], checked_at: "2026-07-25T00:00:00Z" },
      },
    ];
    renderPage();
    const section = selectProject();
    fireEvent.click(within(section).getByRole("button", { name: "Edit" }));

    expect(within(section).queryByText("Source repository")).not.toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Save binding" })).toBeEnabled();
  });

  it("renders provider-schema fields through the app's own field components", () => {
    mockDastBindings = [
      {
        id: 11,
        project: 9,
        target,
        source_repo_key: "source",
        enabled: true,
        parameter_snapshot: { mode: "fast", label: "baseline", count: 2, advanced: false },
        readiness: { ready: true, issues: [], checked_at: "2026-07-25T00:00:00Z" },
      },
    ];
    renderPage();
    const section = selectProject();
    fireEvent.click(within(section).getByRole("button", { name: "Edit" }));

    // An enum field is the app's Radix select — a button trigger with a portalled menu, the
    // same control every other select in the app uses, not a native dropdown drawn by the OS.
    const mode = within(section).getByRole("combobox", { name: /Mode/ });
    expect(mode.tagName).toBe("BUTTON");
    // A boolean field is the shared checkbox, and a text field is the shared input.
    expect(within(section).getByLabelText("Advanced")).toHaveAttribute("type", "checkbox");
    expect(within(section).getByLabelText("Label").tagName).toBe("INPUT");
    // Raw enum values reach the operator readable, not as bare identifiers.
    fireEvent.click(mode);
    expect(screen.getByRole("option", { name: "Fast" })).toBeInTheDocument();
  });

  it("keeps binding metadata controls out of the provider-schema form so RJSF styling cannot reflow them", () => {
    mockDastBindings = [
      {
        id: 11,
        project: 9,
        target,
        source_repo_key: "source",
        enabled: true,
        parameter_snapshot: { mode: "fast", label: "baseline", count: 2, advanced: false },
        readiness: { ready: true, issues: [], checked_at: "2026-07-25T00:00:00Z" },
      },
    ];
    renderPage();
    const section = selectProject();
    fireEvent.click(within(section).getByRole("button", { name: "Edit" }));

    const schemaForm = section.querySelector("form")!;
    const enabled = within(section).getByLabelText("Enabled");
    expect(enabled).toBeInTheDocument();
    expect(schemaForm.contains(enabled)).toBe(false);
    expect(within(section).queryByLabelText("Allow autonomous launches")).not.toBeInTheDocument();
    // The provider-schema fields themselves must stay inside the RJSF form.
    expect(schemaForm.contains(within(section).getByLabelText(/Label/))).toBe(true);
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

describe("OrgIntegrationsPage — DAST catalog sync", () => {
  const dastIntegration = (syncStatus: string, extra: Record<string, string> = {}) => ({
    id: 7,
    name: "Primary DAST gateway",
    integration_type: "DAST",
    is_active: true,
    has_secret: true,
    config: { gateway_url: "https://gateway.example" },
    vpn_integration: null,
    dast_state: { validation_state: "READY", sync_status: syncStatus, sync_error_code: "", ...extra },
  });

  function invalidatedKeys(spy: MockInstance<QueryClient["invalidateQueries"]>) {
    return spy.mock.calls.map(([filters]) => JSON.stringify(filters?.queryKey));
  }

  let queryClient: QueryClient;
  let invalidateSpy: MockInstance<QueryClient["invalidateQueries"]>;

  beforeEach(() => {
    canManage = true;
    mockProjects = [];
    mockDastTargets = [];
    mockDastBindings = [];
    mockValidationStatus = undefined;
    vi.clearAllMocks();
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    invalidateSpy = vi.spyOn(queryClient, "invalidateQueries");
  });

  afterEach(() => cleanup());

  it("keeps Synchronizing until the worker finishes, then refetches DAST data including bindings", async () => {
    mockIntegrations = [dastIntegration("SUCCEEDED")];
    const { rerenderPage } = renderPage(queryClient);

    // The 202 only reserves the sync; the refetched list now reports it in flight.
    syncCapabilitiesMutateAsync.mockImplementationOnce(async () => {
      mockIntegrations = [dastIntegration("PENDING")];
      return { task_id: "sync-1" };
    });
    fireEvent.click(screen.getByRole("button", { name: /Synchronize/ }));
    await waitFor(() => expect(syncCapabilitiesMutateAsync).toHaveBeenCalledWith(7));
    rerenderPage();

    expect(screen.getByRole("button", { name: /Synchronizing…/ })).toBeDisabled();
    mockIntegrations = [dastIntegration("RUNNING")];
    rerenderPage();
    expect(screen.getByRole("button", { name: /Synchronizing…/ })).toBeDisabled();
    expect(invalidatedKeys(invalidateSpy)).not.toContain(JSON.stringify(["dast-bindings"]));
    expect(toastPush).not.toHaveBeenCalled();

    mockIntegrations = [dastIntegration("SUCCEEDED")];
    rerenderPage();

    await waitFor(() => expect(toastPush).toHaveBeenCalledWith("DAST target catalog synchronized.", "success"));
    const keys = invalidatedKeys(invalidateSpy);
    expect(keys).toContain(JSON.stringify(["dast-bindings"]));
    expect(keys).toContain(JSON.stringify(["dast-targets", 1]));
    expect(keys).toContain(JSON.stringify(["org-integrations", 1]));
    expect(screen.getByRole("button", { name: /^Synchronize$/ })).toBeEnabled();
  });

  it("reports a failed sync with its error code", async () => {
    mockIntegrations = [dastIntegration("SUCCEEDED")];
    const { rerenderPage } = renderPage(queryClient);
    syncCapabilitiesMutateAsync.mockImplementationOnce(async () => {
      mockIntegrations = [dastIntegration("RUNNING")];
      return { task_id: "sync-1" };
    });
    fireEvent.click(screen.getByRole("button", { name: /Synchronize/ }));
    await waitFor(() => expect(syncCapabilitiesMutateAsync).toHaveBeenCalled());
    rerenderPage();

    mockIntegrations = [dastIntegration("FAILED", { sync_error_code: "CATALOG_INVALID" })];
    rerenderPage();

    await waitFor(() => expect(toastPush).toHaveBeenCalledWith("Synchronization failed: CATALOG_INVALID", "error"));
    expect(invalidatedKeys(invalidateSpy)).toContain(JSON.stringify(["dast-bindings"]));
  });

  it("shows a sync started elsewhere (periodic refresh) as in progress without a toast", () => {
    mockIntegrations = [dastIntegration("RUNNING")];

    renderPage(queryClient);

    expect(screen.getByRole("button", { name: /Synchronizing…/ })).toBeDisabled();
    expect(toastPush).not.toHaveBeenCalled();
  });

  it("lets the operator retry once an in-flight sync is reported stalled", () => {
    mockIntegrations = [dastIntegration("STALLED")];

    renderPage(queryClient);

    expect(screen.getByRole("button", { name: /^Synchronize$/ })).toBeEnabled();
  });

  it("after a successful Validate waits for the chained catalog sync before refreshing DAST data", async () => {
    mockIntegrations = [dastIntegration("SUCCEEDED")];
    const { rerenderPage } = renderPage(queryClient);

    fireEvent.click(screen.getByRole("button", { name: /Validate/ }));
    // Validation READY is committed together with the reserved sync.
    mockIntegrations = [dastIntegration("PENDING")];
    mockValidationStatus = { state: "READY", valid: true, detail: "" };
    rerenderPage();

    await waitFor(() => expect(toastPush).toHaveBeenCalledWith("Credentials are valid.", "success"));
    await waitFor(() => expect(screen.getByRole("button", { name: /Synchronizing…/ })).toBeDisabled());
    expect(invalidatedKeys(invalidateSpy)).not.toContain(JSON.stringify(["dast-bindings"]));

    mockIntegrations = [dastIntegration("SUCCEEDED")];
    rerenderPage();

    await waitFor(() => expect(toastPush).toHaveBeenCalledWith("DAST target catalog synchronized.", "success"));
    expect(invalidatedKeys(invalidateSpy)).toContain(JSON.stringify(["dast-bindings"]));
  });
});

describe("OrgIntegrationsPage — build environment integration", () => {
  const storedConan = {
    id: 21,
    name: "conan",
    integration_type: "BUILD_ENV",
    is_active: true,
    has_secret: false,
    config: {},
    vpn_integration: null,
    build_env: [
      { name: "CONAN_LOGIN_USERNAME_ACME", has_value: true },
      { name: "CONAN_PASSWORD_ACME", has_value: true },
      { name: "OLD_TOKEN", has_value: true },
    ],
  };

  beforeEach(() => {
    mockIntegrations = [];
    mockProjects = [];
    mockOverrides = [];
    canManage = true;
    vi.clearAllMocks();
  });

  afterEach(() => cleanup());

  function startNewBuildEnv() {
    startNewIntegration("Build environment", "conan");
  }

  function addVariable(name: string, value: string) {
    fireEvent.click(screen.getByRole("button", { name: "+ Add variable" }));
    const names = screen.getAllByLabelText("Variable name");
    const values = screen.getAllByPlaceholderText("value");
    fireEvent.change(names[names.length - 1], { target: { value: name } });
    fireEvent.change(values[values.length - 1], { target: { value } });
  }

  it("creates the integration with its variables and no access token", async () => {
    renderPage();
    startNewBuildEnv();
    expect(screen.queryByText("Access Token")).not.toBeInTheDocument();

    addVariable("CONAN_LOGIN_USERNAME_ACME", "ci-bot");
    addVariable("CONAN_PASSWORD_ACME", "remote-password");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createIntegrationMutateAsync).toHaveBeenCalledTimes(1));
    const payload = createIntegrationMutateAsync.mock.calls[0][0];
    expect(payload).toMatchObject({
      integration_type: "BUILD_ENV",
      name: "conan",
      build_env: [
        { name: "CONAN_LOGIN_USERNAME_ACME", value: "ci-bot" },
        { name: "CONAN_PASSWORD_ACME", value: "remote-password" },
      ],
    });
    expect(payload).not.toHaveProperty("secret");
  });

  it("cannot save a new variable without a value", () => {
    renderPage();
    startNewBuildEnv();

    addVariable("CONAN_PASSWORD_ACME", "");

    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
  });

  it("lists the integration with its variable count", () => {
    mockIntegrations = [storedConan];

    renderPage();

    expect(within(orgSection()).getByText("conan")).toBeInTheDocument();
    expect(within(orgSection()).getByText("3 variables")).toBeInTheDocument();
  });

  it("rotates one value, keeps another and removes a third in one save", async () => {
    mockIntegrations = [storedConan];
    renderPage();
    fireEvent.click(within(orgSection()).getByRole("button", { name: "Edit" }));

    const names = screen.getAllByLabelText("Variable name");
    expect(names.map((input) => (input as HTMLInputElement).value)).toEqual([
      "CONAN_LOGIN_USERNAME_ACME",
      "CONAN_PASSWORD_ACME",
      "OLD_TOKEN",
    ]);
    names.forEach((input) => expect(input).toBeDisabled());
    const values = screen.getAllByPlaceholderText("stored — leave blank to keep");
    values.forEach((input) => expect((input as HTMLInputElement).value).toBe(""));

    fireEvent.change(values[1], { target: { value: "rotated-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Remove OLD_TOKEN" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateIntegrationMutateAsync).toHaveBeenCalledTimes(1));
    expect(updateIntegrationMutateAsync.mock.calls[0][0]).toMatchObject({
      integrationId: 21,
      payload: {
        build_env: [
          { name: "CONAN_LOGIN_USERNAME_ACME" },
          { name: "CONAN_PASSWORD_ACME", value: "rotated-password" },
        ],
      },
    });
    expect(updateIntegrationMutateAsync.mock.calls[0][0].payload.build_env[0]).not.toHaveProperty("value");
  });

  it("lets a project switch the build environment off", async () => {
    mockIntegrations = [storedConan];
    mockProjects = [{ id: 9, name: "acme-client", organizationId: 1, productId: 3, canOperate: true }];
    renderPage();

    const overrides = screen.getByText("Per-Project Overrides").closest("section")!;
    fireEvent.click(within(overrides).getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: "acme-client" }));
    fireEvent.click(within(overrides).getByLabelText("Disable Build environment"));

    await waitFor(() =>
      expect(setOverrideMutateAsync).toHaveBeenCalledWith({ integrationType: "BUILD_ENV", isDisabled: true }),
    );
    expect(toastPush).toHaveBeenCalledWith("Build environment disabled for this project.", "success");
  });

  it("shows a project's overrides read-only to someone who cannot operate the project", () => {
    mockIntegrations = [storedConan];
    mockProjects = [{ id: 9, name: "acme-client", organizationId: 1, productId: 3, canOperate: false }];
    mockOverrides = [{
      id: 5, project: 9, integration_type: "BUILD_ENV", org_integration: 21, config_override: {}, is_disabled: false,
    }];
    renderPage();

    const overrides = screen.getByText("Per-Project Overrides").closest("section")!;
    fireEvent.click(within(overrides).getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: "acme-client" }));

    expect(within(overrides).getByText(/changing them requires Maintainer or Owner on the project/)).toBeInTheDocument();
    const [, buildEnvPicker] = within(overrides).getAllByRole("combobox");
    expect(buildEnvPicker).toBeDisabled();
    expect(within(overrides).getByLabelText("Disable Build environment")).toBeDisabled();
    expect(within(overrides).queryByTitle("Clear override")).not.toBeInTheDocument();
  });

  it("lets a project switch a disabled build environment back on", async () => {
    mockIntegrations = [storedConan];
    mockProjects = [{ id: 9, name: "acme-client", organizationId: 1, productId: 3, canOperate: true }];
    mockOverrides = [{
      id: 5, project: 9, integration_type: "BUILD_ENV", org_integration: null, config_override: {}, is_disabled: true,
    }];
    renderPage();

    const overrides = screen.getByText("Per-Project Overrides").closest("section")!;
    fireEvent.click(within(overrides).getByRole("combobox"));
    fireEvent.click(screen.getByRole("option", { name: "acme-client" }));
    expect(within(overrides).getByText("disabled for this project")).toBeInTheDocument();
    fireEvent.click(within(overrides).getByLabelText("Disable Build environment"));

    await waitFor(() => expect(deleteOverrideMutateAsync).toHaveBeenCalledWith("BUILD_ENV"));
    expect(toastPush).toHaveBeenCalledWith(
      "Build environment override cleared — org default will be used.",
      "success",
    );
  });
});

describe("OrgIntegrationsPage — source control, VPN, Claude and notification integrations", () => {
  const officeVpn = {
    id: 5,
    name: "Office VPN",
    integration_type: "VPN",
    is_active: true,
    has_secret: false,
    config: {},
    vpn_integration: null,
  };

  beforeEach(() => {
    mockIntegrations = [];
    canManage = true;
    vi.clearAllMocks();
  });

  afterEach(() => cleanup());

  it("creates a self-hosted GitLab integration routed through the organization's VPN", async () => {
    mockIntegrations = [officeVpn];
    renderPage();
    const section = startNewIntegration("GitLab", "Internal GitLab");
    fireEvent.change(within(section).getByPlaceholderText("https://gitlab.com"), {
      target: { value: " https://git.example.test " },
    });
    fireEvent.change(within(section).getByLabelText("Access Token"), { target: { value: " glpat-token " } });
    fireEvent.click(within(section).getByRole("combobox", { name: "VPN Integration" }));
    fireEvent.click(screen.getByRole("option", { name: "Office VPN" }));
    fireEvent.click(within(section).getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createIntegrationMutateAsync).toHaveBeenCalledTimes(1));
    expect(createIntegrationMutateAsync).toHaveBeenCalledWith({
      integration_type: "GITLAB",
      name: "Internal GitLab",
      config: { base_url: "https://git.example.test" },
      is_active: true,
      vpn_integration: 5,
      secret: "glpat-token",
    });
  });

  it("updates a VPN integration with only the credentials the operator entered", async () => {
    mockIntegrations = [officeVpn];
    renderPage();
    const section = orgSection();
    fireEvent.click(within(section).getByRole("button", { name: /edit/i }));
    expect(within(section).queryByText("VPN Integration")).not.toBeInTheDocument();
    fireEvent.change(within(section).getByLabelText("Username"), { target: { value: " ci-bot " } });
    fireEvent.change(within(section).getByPlaceholderText("10.0.0.1"), { target: { value: "10.8.0.1" } });
    fireEvent.click(within(section).getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateIntegrationMutateAsync).toHaveBeenCalledTimes(1));
    const { integrationId, payload } = updateIntegrationMutateAsync.mock.calls[0][0];
    expect(integrationId).toBe(5);
    expect(payload).toEqual({
      integration_type: "VPN",
      name: "Office VPN",
      config: { ping_target: "10.8.0.1" },
      is_active: true,
      vpn_secret: { vpn_username: "ci-bot" },
    });
    expect(payload).not.toHaveProperty("secret");
    expect(payload.vpn_integration).toBeUndefined();
  });

  it("creates a Claude Code integration that authenticates with the pasted OAuth token", async () => {
    renderPage();
    const section = startNewIntegration("Claude Code", "Triage");
    expect(within(section).getByText("claude setup-token")).toBeInTheDocument();
    fireEvent.change(within(section).getByLabelText("Access Token"), { target: { value: "oauth-token" } });
    fireEvent.click(within(section).getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createIntegrationMutateAsync).toHaveBeenCalledTimes(1));
    expect(createIntegrationMutateAsync).toHaveBeenCalledWith({
      integration_type: "CLAUDE_CODE",
      name: "Triage",
      config: { auth_mode: "oauth" },
      is_active: true,
      vpn_integration: null,
      secret: "oauth-token",
    });
  });

  it("asks for the Slack default channel before a Slack integration can be created", () => {
    renderPage();
    const section = startNewIntegration("Slack", "Alerts");
    expect(within(section).getByText("Bot Token")).toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Create" })).toBeDisabled();

    fireEvent.change(within(section).getByPlaceholderText("#alerts or channel ID"), { target: { value: "#security" } });

    expect(within(section).getByRole("button", { name: "Create" })).toBeEnabled();
  });

  it("creates an Email integration from its sender address, without a token", async () => {
    renderPage();
    const section = startNewIntegration("Email", "Mailer");
    expect(within(section).queryByText(/Token/)).not.toBeInTheDocument();
    expect(within(section).getByRole("button", { name: "Create" })).toBeDisabled();

    fireEvent.change(within(section).getByPlaceholderText("noreply@example.com"), {
      target: { value: "security@example.test" },
    });
    fireEvent.click(within(section).getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createIntegrationMutateAsync).toHaveBeenCalledTimes(1));
    expect(createIntegrationMutateAsync).toHaveBeenCalledWith({
      integration_type: "EMAIL",
      name: "Mailer",
      config: { from_email: "security@example.test" },
      is_active: true,
      vpn_integration: null,
    });
  });
});
