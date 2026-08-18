export type RiskState = "risk_accepted" | "under_review" | "mitigated";
export type Severity = "Critical" | "High" | "Medium" | "Low" | "Info";
export type AIVerdict = "true_positive" | "false_positive" | "uncertain";
/**
 * DAST_TARGET stands for a scan target rather than a revision: it is created by the import that
 * produces its findings and is never selectable as the source of an analysis run.
 */
export type ProjectVersionType = "GIT_BRANCH" | "GIT_HASH" | "FILE_HASH" | "DAST_TARGET";

export type WorkItemStatusCategory = "OPEN" | "IN_PROGRESS" | "DONE" | "CANCELLED" | "UNKNOWN";

export type WorkItemLink = {
  id: number;
  externalKey: string;
  externalUrl: string;
  title: string;
  statusCategory: WorkItemStatusCategory;
  providerName: string | null;
  providerType: string | null;
  provider: number | null;
};

export type FindingFilters = {
  projectId?: number;
  pipelineId?: string;
  title?: string;
  createdGte?: string;
  createdLte?: string;
  statusUpdatedGte?: string;
  statusUpdatedLte?: string;
  processedGte?: string;
  processedLte?: string;
  mitigatedGte?: string;
  mitigatedLte?: string;
  projectVersion?: string;
  file?: string;
  severities?: Severity[];
  status?: "enabled" | "disabled";
  riskStates?: RiskState[];
  aiStatus?: "has_ai" | "no_ai" | "ai_tp" | "ai_fp" | "ai_u";
  aiVerdict?: AIVerdict;
  cwe?: string;
  tags?: string[];
  workItemStatus?: string;
  limit?: number;
  offset?: number;
  ordering?: string;
};

export type Finding = {
  id: number;
  title: string;
  severity: Severity;
  active: boolean;
  isMitigated?: boolean;
  riskAccepted?: boolean;
  falsePositive?: boolean;
  outOfScope?: boolean;
  duplicate?: boolean;
  product: string;
  projectId?: number;
  date?: string;
  createdAt?: string;
  filePath: string;
  line: number | null;
  tool: string;
  description?: string;
  cwe?: number | null;
  tags?: string[];
  testId?: number | null;
  aiVerdict?: AIVerdict;
  snippetPreview?: string;
  riskStates?: RiskState[];
  projectVersionId?: number;
  projectVersion?: string;
  projectVersionType?: ProjectVersionType;
  sourceFileLink?: string;
  lastStatusUpdate?: string;
  isRegression?: boolean;
  workItems?: WorkItemLink[];
  dynamicFinding?: boolean;
  affectedEndpoints?: string[];
  stepsToReproduce?: string;
  impact?: string;
  mitigation?: string;
  param?: string;
  payload?: string;
  cvssv3?: string;
  cvssv3Score?: number;
  references?: string;
};

export type AiFixType = "code_change" | "config_change" | "architectural";

export type AiFix = {
  fixSummary: string;
  fixType: AiFixType;
  diff?: string | null;
  diffAvailable: boolean;
  codeAfter?: string | null;
  stepByStep: string[];
  testingHint?: string | null;
  secretsManagement?: string | null;
  suppressionAnnotation?: string | null;
};

export type AIResponse = {
  verdict?: AIVerdict;
  title?: string;
  reasoning: string;
  epssScore?: number;
  impactScore?: number;
  exploitabilityScore?: number;
  uncertaintyLevel?: number;
  uncertaintySpread?: number;
  exploitCodeMaturity?: string;
  references?: string[];
  pipelineId?: string;
  fix?: AiFix | null;
};

export type Note = {
  id: number;
  entry: string;
  user_display?: string;
  author_name?: string;
  author?: {
    username?: string;
    first_name?: string;
    last_name?: string;
  };
  date?: string;
};

export type Project = {
  id: number;
  productId: number;
  name: string;
  organizationId?: number | null;
  organizationName?: string | null;
};

export type RiskScore = {
  score: number;
  label: "critical" | "high" | "medium" | "low";
};

export type ProductSummary = {
  projectId: number;
  productId: number;
  name: string;
  tags: string[];
  status: "active" | "inactive";
  findingsTotal: number;
  findingsActive: number;
  severity: Record<Severity, number>;
  risk: {
    riskAccepted: number;
    underReview: number;
    mitigated: number;
  };
  riskScore?: RiskScore;
  lastPipeline?: {
    id?: string | null;
    status?: string | null;
    updated?: string | null;
  };
  lastSync?: string | null;
};

/**
 * Provider-reported coverage and token usage from an accepted DAST report. Every field is
 * optional at the source, so null means "the report did not carry it" — never zero.
 */
export type DastRunSummary = {
  runId: string;
  runType: string | null;
  coverageUnit: string | null;
  discovered: number | null;
  reachable: number | null;
  analysed: number | null;
  planned: number | null;
  beyondPlan: number | null;
  totalTokens: number | null;
  modelCalls: number | null;
};

export type DastTokenBucket = {
  key: string;
  name?: string | null;
  agents?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  thinking_tokens?: number | null;
  cache_creation_tokens?: number | null;
  cache_read_tokens?: number | null;
  calls?: number | null;
  total_tokens: number | null;
};

export type DastRunDetail = DastRunSummary & {
  targetId: string;
  standId: string;
  productFamily: string | null;
  tier: string | null;
  targetHost: string | null;
  scanStarted: string | null;
  scanFinished: string | null;
  durationSeconds: number | null;
  analysedNames: string[] | null;
  beyondPlanNames: string[] | null;
  tokens: {
    input_tokens: number | null;
    output_tokens: number | null;
    thinking_tokens: number | null;
    cache_creation_tokens: number | null;
    cache_read_tokens: number | null;
  };
  tokenByPhase: DastTokenBucket[] | null;
  tokenByAgentType: DastTokenBucket[] | null;
  agents: number | null;
  tokenAccountingConsistent: boolean | null;
};

export type PipelineSummary = {
  id: string;
  executionType: "SAST" | "DAST" | "MANUAL_IMPORT";
  status: string;
  dastOutcomeCode?: import("./lib/dastNarrative").DastOutcomeCode | null;
  dastRun?: DastRunSummary | null;
  projectId: number;
  productId: number;
  productName: string;
  started?: string | null;
  created?: string | null;
  updated?: string | null;
  branch?: string | null;
  commit?: string | null;
  findings: number;
  actions: Array<{
    source?: string | null;
    type?: string | null;
    status?: string | null;
    updated?: string | null;
  }>;
};

export type CalendarEventType =
  | "pipeline_started"
  | "pipeline_scheduled"
  | "finding_created"
  | "finding_processed"
  | "project_created";

export type CalendarView = "day" | "week" | "month";

export type CalendarEvent = {
  id: string;
  eventType: CalendarEventType;
  title: string;
  start: string;
  end?: string | null;
  isAllDay: boolean;
  isAggregated: boolean;
  count: number;
  isFuture: boolean;
  colorVariant: string;
  summary: Record<string, unknown>;
  link?: string | null;
};

export type FindingTimelineEventType =
  | "finding_created"
  | "finding_processed"
  | "finding_note_added";

export type FindingTimelineEvent = {
  id: string;
  eventType: FindingTimelineEventType;
  happenedAt: string;
  findingId: number;
  title: string;
  severity: string;
  projectIds: number[];
  processedReason?: string | null;
  owner?: string | null;
  details?: string | null;
  link: string;
};
