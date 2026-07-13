// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { OrgMember } from "../lib/orgMembers";

const pushToast = vi.fn();
const changeRoleMutateAsync = vi.fn();
const inviteMutateAsync = vi.fn();

let mockManageableOrgs: { id: number; name: string }[] = [];
let mockMembers: OrgMember[] = [];
let mockProjects: { id: number; name: string; organizationId: number | null }[] = [];
let mockChangeRolePending = false;
let mockInvitePending = false;
let mockResetPasswordPending = false;
let mockResetAccessPending = false;

const resetAccessMutateAsync = vi.fn();

vi.mock("../lib/queries", () => ({
  useManageableOrgs: () => ({ data: mockManageableOrgs, isLoading: false }),
  useProjects: () => ({ data: mockProjects, isLoading: false }),
}));

vi.mock("../lib/orgMembers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/orgMembers")>();
  return {
    ...actual,
    useOrgMembers: () => ({ data: mockMembers, isLoading: false }),
    useChangeMemberRole: () => ({ mutateAsync: changeRoleMutateAsync, isPending: mockChangeRolePending }),
    useInviteMember: () => ({ mutateAsync: inviteMutateAsync, isPending: mockInvitePending }),
    useRemoveMember: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useResetMemberPassword: () => ({ mutateAsync: vi.fn(), isPending: mockResetPasswordPending }),
    useResetOrgMemberAccess: () => ({ mutateAsync: resetAccessMutateAsync, isPending: mockResetAccessPending }),
    useGrantProject: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useRevokeProject: () => ({ mutateAsync: vi.fn(), isPending: false }),
  };
});

vi.mock("../components/ToastProvider", () => ({
  useToast: () => ({ push: pushToast }),
}));

import UsersPage from "./UsersPage";

const FULL_MEMBER: OrgMember = {
  user_id: 7,
  username: "jane.doe",
  email: "jane.doe@example.com",
  first_name: "Jane",
  last_name: "Doe",
  is_active: true,
  role_id: 4,
  role_name: "Owner",
  membership_type: "full",
  has_token: false,
  token_count: 0,
  project_grants: [],
  denied_project_ids: [],
};

afterEach(() => {
  cleanup();
});

const RESTRICTED_MEMBER: OrgMember = {
  ...FULL_MEMBER,
  user_id: 8,
  username: "restricted.member",
  email: "restricted.member@example.com",
  first_name: "Restricted",
  last_name: "Member",
  membership_type: "restricted",
  project_grants: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  mockManageableOrgs = [];
  mockMembers = [];
  mockProjects = [];
  mockChangeRolePending = false;
  mockInvitePending = false;
  mockResetPasswordPending = false;
  mockResetAccessPending = false;
});

describe("UsersPage — gated on org-membership management, not shown as a bare admin panel", () => {
  it("shows a no-access message and renders none of the member/invite admin UI when the user manages no organizations", () => {
    mockManageableOrgs = [];
    render(<UsersPage />);
    expect(screen.getByText("You do not manage any organizations.")).toBeInTheDocument();
    expect(screen.queryByText("Invite member")).not.toBeInTheDocument();
    expect(screen.queryByText("Members")).not.toBeInTheDocument();
  });

  it("renders the member list once the user manages at least one organization", () => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    mockMembers = [FULL_MEMBER];
    render(<UsersPage />);
    expect(screen.getByText("Jane Doe")).toBeInTheDocument();
    expect(screen.getByText("Invite member")).toBeInTheDocument();
  });
});

describe("UsersPage — role-change select is guarded against concurrent double-submit", () => {
  beforeEach(() => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    mockMembers = [FULL_MEMBER];
  });

  it("disables the role select while a role change is pending", () => {
    mockChangeRolePending = true;
    render(<UsersPage />);
    const roleSelect = screen.getAllByRole("combobox")[0];
    expect(roleSelect).toBeDisabled();
  });

  it("leaves the role select enabled when no role change is in flight", () => {
    mockChangeRolePending = false;
    render(<UsersPage />);
    const roleSelect = screen.getAllByRole("combobox")[0];
    expect(roleSelect).not.toBeDisabled();
  });
});

describe("UsersPage — invite form is guarded against double-submit", () => {
  beforeEach(() => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    mockMembers = [];
  });

  it("disables Send invite and the email field while an invite is pending", () => {
    mockInvitePending = true;
    render(<UsersPage />);
    fireEvent.click(screen.getByText("Invite member"));
    expect(screen.getByRole("button", { name: "Send invite" })).toBeDisabled();
    expect(screen.getByPlaceholderText("member@example.com")).toBeDisabled();
  });

  it("leaves Send invite enabled when no invite is in flight", () => {
    mockInvitePending = false;
    render(<UsersPage />);
    fireEvent.click(screen.getByText("Invite member"));
    expect(screen.getByRole("button", { name: "Send invite" })).not.toBeDisabled();
  });
});

describe("UsersPage — Reset password is guarded against rapid repeat clicks", () => {
  beforeEach(() => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    mockMembers = [FULL_MEMBER];
  });

  it("disables Reset password while a reset for this member is pending", () => {
    mockResetPasswordPending = true;
    render(<UsersPage />);
    expect(screen.getByRole("button", { name: "Reset password" })).toBeDisabled();
  });
});

describe("UsersPage — Access drawer offers an explicit reset for restricted members", () => {
  beforeEach(() => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
  });

  it("shows 'Reset to full access' for a restricted member, not the full-member info text", () => {
    mockMembers = [RESTRICTED_MEMBER];
    render(<UsersPage />);
    fireEvent.click(screen.getByText("Manage access"));
    expect(screen.getByRole("button", { name: "Reset to full access" })).toBeInTheDocument();
    expect(screen.queryByText(/already see every project/)).not.toBeInTheDocument();
  });

  it("does not show 'Reset to full access' for a full member", () => {
    mockMembers = [FULL_MEMBER];
    render(<UsersPage />);
    fireEvent.click(screen.getByText("Manage access"));
    expect(screen.queryByRole("button", { name: "Reset to full access" })).not.toBeInTheDocument();
    expect(screen.getByText(/already see every project/)).toBeInTheDocument();
  });

  it("resets a restricted member to full access after confirmation", async () => {
    mockMembers = [RESTRICTED_MEMBER];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<UsersPage />);
    fireEvent.click(screen.getByText("Manage access"));
    fireEvent.click(screen.getByRole("button", { name: "Reset to full access" }));
    await waitFor(() => expect(resetAccessMutateAsync).toHaveBeenCalledWith(RESTRICTED_MEMBER.user_id));
  });

  it("does nothing when the reset confirmation is dismissed", () => {
    mockMembers = [RESTRICTED_MEMBER];
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<UsersPage />);
    fireEvent.click(screen.getByText("Manage access"));
    fireEvent.click(screen.getByRole("button", { name: "Reset to full access" }));
    expect(resetAccessMutateAsync).not.toHaveBeenCalled();
  });
});

describe("UsersPage — Access drawer caps a full member's per-project role at their org role", () => {
  // Regression: the per-project selector used to offer every role
  // unconditionally, so a full member's drawer let you pick a role above
  // their org-wide role — the backend correctly rejects that with a 400
  // (service.py's _grant_project), but the option looked selectable.
  beforeEach(() => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    mockProjects = [{ id: 1, name: "payments-api", organizationId: 1 }];
  });

  it("greys out roles above the org role for a full member", () => {
    mockMembers = [{ ...FULL_MEMBER, role_id: 2, role_name: "Writer" }];
    render(<UsersPage />);
    fireEvent.click(screen.getByText("Manage access"));

    // The one row's role select, inside the drawer's ProjectAccessEditor.
    fireEvent.click(screen.getAllByRole("combobox").slice(-1)[0]);

    expect(screen.getByRole("option", { name: "Reader" }).getAttribute("aria-disabled")).not.toBe("true");
    expect(screen.getByRole("option", { name: "Writer" }).getAttribute("aria-disabled")).not.toBe("true");
    expect(screen.getByRole("option", { name: "Maintainer" }).getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByRole("option", { name: "Owner" }).getAttribute("aria-disabled")).toBe("true");
  });

  it("does not cap roles for a restricted member", () => {
    mockMembers = [{ ...RESTRICTED_MEMBER, role_id: 2 }];
    render(<UsersPage />);
    fireEvent.click(screen.getByText("Manage access"));
    fireEvent.click(screen.getAllByRole("combobox").slice(-1)[0]);

    expect(screen.getByRole("option", { name: "Owner" }).getAttribute("aria-disabled")).not.toBe("true");
  });
});

describe("UsersPage — member name/email are rendered as literal text, not markup", () => {
  const XSS_NAME = "<script>alert(1)</script>";
  const XSS_EMAIL = "<img src=x onerror=alert(1)>@example.com";

  it("renders a malicious first_name as escaped text in the member row", () => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    mockMembers = [{ ...FULL_MEMBER, first_name: XSS_NAME, last_name: "" }];
    render(<UsersPage />);

    const nameNode = screen.getByText(XSS_NAME);
    expect(nameNode.textContent).toBe(XSS_NAME);
    expect(document.querySelector("script")).toBeNull();
    expect(document.body.innerHTML).not.toContain("<script>alert(1)</script>");
  });

  it("renders a malicious email as escaped text in the member row", () => {
    mockManageableOrgs = [{ id: 1, name: "Acme" }];
    mockMembers = [{ ...FULL_MEMBER, email: XSS_EMAIL }];
    render(<UsersPage />);

    const emailNode = screen.getByText(XSS_EMAIL);
    expect(emailNode.textContent).toBe(XSS_EMAIL);
    expect(document.querySelector("img[onerror]")).toBeNull();
  });
});
