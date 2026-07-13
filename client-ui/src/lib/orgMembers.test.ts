// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { inviteResultMessage, type InviteResult } from "./orgMembers";

const BASE: InviteResult = {
  user_id: 1,
  username: "new.user",
  email: "new.user@example.com",
  invite_status: "invited",
};

describe("inviteResultMessage", () => {
  it("reports a real invite as an email being sent", () => {
    expect(inviteResultMessage(BASE)).toBe("Invitation sent.");
  });

  it("does not claim an email was sent when an existing user was silently added", () => {
    const result: InviteResult = { ...BASE, invite_status: "existing_user_added_no_email" };
    const message = inviteResultMessage(result);
    expect(message).not.toBe("Invitation sent.");
    expect(message).toContain(result.email);
    expect(message.toLowerCase()).toContain("no email was sent");
  });
});
