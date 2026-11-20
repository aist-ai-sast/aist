/** Organization integration types, in the order the type picker offers them. */
export const ORG_INTEGRATION_TYPES = [
  "GITLAB",
  "GITHUB",
  "GERRIT",
  "GITEA",
  "SLACK",
  "EMAIL",
  "VPN",
  "CLAUDE_CODE",
  "DAST",
  "BUILD_ENV",
] as const;

export type IntegrationType = (typeof ORG_INTEGRATION_TYPES)[number];
