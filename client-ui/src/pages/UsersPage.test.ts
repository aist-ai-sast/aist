// @vitest-environment jsdom
import { describe, expect, it } from "vitest";

import { buildRoleByProject } from "./UsersPage";

const PROJECTS = [{ id: 1 }, { id: 2 }, { id: 3 }];

describe("buildRoleByProject", () => {
  it("seeds every project with the org-wide role for a full member instead of No access", () => {
    const member = { membership_type: "full" as const, role_id: 4, project_grants: [], denied_project_ids: [] };
    const result = buildRoleByProject(member, PROJECTS);
    expect(result).toEqual({ 1: 4, 2: 4, 3: 4 });
  });

  it("uses only explicit grants for a restricted member, leaving other projects unseeded", () => {
    const member = {
      membership_type: "restricted" as const,
      role_id: 5,
      project_grants: [{ project_id: 2, product_id: 1, project_name: "p2", role_id: 3, role_name: "Maintainer" }],
      denied_project_ids: [],
    };
    const result = buildRoleByProject(member, PROJECTS);
    expect(result).toEqual({ 2: 3 });
  });

  it("seeds nothing for a restricted member with zero grants, instead of falling back to the org role", () => {
    // Regression: this is the exact post-fix state of a member who had every
    // project revoked — an empty grants list must render every row as "No
    // access", never as if they were still a full member.
    const member = { membership_type: "restricted" as const, role_id: 4, project_grants: [], denied_project_ids: [] };
    const result = buildRoleByProject(member, PROJECTS);
    expect(result).toEqual({});
  });

  it("denies only the one explicitly denied project for a full member, leaving the rest at the org role", () => {
    // Regression: revoking access on ONE project used to flip the member
    // into allow-list mode, showing every OTHER, untouched project as "No
    // access" too. A denial must only affect its own project.
    const member = {
      membership_type: "full" as const,
      role_id: 4,
      project_grants: [],
      denied_project_ids: [2],
    };
    const result = buildRoleByProject(member, PROJECTS);
    expect(result).toEqual({ 1: 4, 2: null, 3: 4 });
  });

  it("downgrades only the one explicitly granted project for a full member, leaving the rest at the org role", () => {
    const member = {
      membership_type: "full" as const,
      role_id: 4,
      project_grants: [{ project_id: 3, product_id: 1, project_name: "p3", role_id: 5, role_name: "Reader" }],
      denied_project_ids: [],
    };
    const result = buildRoleByProject(member, PROJECTS);
    expect(result).toEqual({ 1: 4, 2: 4, 3: 5 });
  });
});
