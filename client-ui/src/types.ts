export type RiskState = "risk_accepted" | "under_review" | "mitigated";
export type Severity = "Critical" | "High" | "Medium" | "Low" | "Info";
export type AIVerdict = "true_positive" | "false_positive" | "uncertain";

export type FindingFilters = {
  productId?: number;
  pipelineId?: string;
  severity?: Severity;
  status?: "enabled" | "disabled";
  riskStates?: RiskState[];
  aiVerdict?: AIVerdict;
  cwe?: string;
  tags?: string[];
  limit?: number;
  offset?: number;
  ordering?: string;
};

export type Finding = {
  id: number;
  title: string;
  severity: Severity;
  active: boolean;
  product: string;
  productId?: number;
  date?: string;
  filePath: string;
  line: number;
  tool: string;
  description?: string;
  cwe?: number | null;
  tags?: string[];
  testId?: number | null;
  aiVerdict?: AIVerdict;
  snippetPreview?: string;
  riskStates?: RiskState[];
  projectVersionId?: number;
  sourceFileLink?: string;
};

export type AIResponse = {
  reasoning: string;
  epssScore?: number;
  impactScore?: number;
  exploitabilityScore?: number;
  references?: string[];
};

export type Note = {
  id: number;
  entry: string;
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
  lastPipeline?: {
    id?: string | null;
    status?: string | null;
    updated?: string | null;
  };
  lastSync?: string | null;
};

export type PipelineSummary = {
  id: string;
  status: string;
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
