// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { AccountProfile } from "../lib/account";
import type { UserProfile } from "../lib/auth";

const updateAccountMutateAsync = vi.fn();
const changePasswordMutateAsync = vi.fn();

let mockAuthData: UserProfile = { username: "jane.doe" };
let mockAccountData: AccountProfile;

vi.mock("../lib/auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/auth")>();
  return {
    ...actual,
    useAuthStatus: () => ({ data: mockAuthData, isLoading: false, isError: false }),
  };
});

vi.mock("../lib/account", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/account")>();
  return {
    ...actual,
    useAccountProfile: () => ({ data: mockAccountData, isLoading: false, isError: false }),
    useUpdateAccountProfile: () => ({ mutateAsync: updateAccountMutateAsync, isPending: false }),
    useChangePassword: () => ({ mutateAsync: changePasswordMutateAsync, isPending: false }),
    logoutAllDevices: vi.fn(),
  };
});

vi.mock("../lib/apiTokens", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/apiTokens")>();
  return {
    ...actual,
    useMyTokens: () => ({ data: [], isLoading: false }),
    useCreateToken: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useDeleteToken: () => ({ mutateAsync: vi.fn(), isPending: false }),
  };
});

vi.mock("../components/ToastProvider", () => ({
  useToast: () => ({ push: vi.fn() }),
}));

import SettingsPage from "./SettingsPage";

afterEach(() => {
  cleanup();
});

describe("SettingsPage — organization membership name is rendered as literal text, not markup", () => {
  it("renders a malicious organization_name as escaped text", () => {
    const xssName = "<script>alert(1)</script>";
    mockAuthData = { user: { username: "jane.doe" } };
    mockAccountData = {
      username: "jane.doe",
      first_name: "Jane",
      last_name: "Doe",
      email: "jane.doe@example.com",
      can_edit_profile: false,
      can_edit_username: false,
      can_create_write_token: false,
      is_superuser: false,
      organization_memberships: [
        {
          organization_id: 1,
          organization_name: xssName,
          role_id: 4,
          role_name: "Owner",
          can_write_findings: true,
          can_operate_projects: true,
          can_manage_access: true,
          can_grant_owner: true,
        },
      ],
    };

    render(<SettingsPage />);

    const nameNodes = screen.getAllByText(xssName);
    expect(nameNodes.length).toBeGreaterThan(0);
    nameNodes.forEach((nameNode) => expect(nameNode.textContent).toBe(xssName));
    expect(document.querySelector("script")).toBeNull();
    expect(document.body.innerHTML).not.toContain("<script>alert(1)</script>");
  });
});
