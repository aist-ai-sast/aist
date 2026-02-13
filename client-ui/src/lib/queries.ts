import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import type { AIResponse, Finding, FindingFilters, Note, PipelineSummary, ProductSummary, Project } from "../types";
import { fetchJson, normalizeList } from "./api";
import { getRoute } from "./routes";

type FindingApi = {
  id: number;
  title: string;
  severity: "Critical" | "High" | "Medium" | "Low" | "Info";
  active: boolean;
  product: number;
  date?: string;
  file_path?: string | null;
  line?: number | null;
  description?: string | null;
  cwe?: number | null;
  tags?: string[];
  test?: number;
  risk_accepted?: boolean;
  under_review?: boolean;
  is_mitigated?: boolean;
  false_p?: boolean;
  out_of_scope?: boolean;
  duplicate?: boolean;
  found_by?: number[];
  last_status_update?: string;
  description?: string;
  finding_meta?: { name: string; value: string }[];
};

type AistProjectApi = {
  id: number;
  product_id: number;
  product_name: string;
};

type ProductSummaryApi = {
  project_id: number;
  product_id: number;
  product_name: string;
  tags: string[];
  status: "active" | "inactive";
  findings_total: number;
  findings_active: number;
  severity: Record<string, number>;
  risk: {
    risk_accepted: number;
    under_review: number;
    mitigated: number;
  };
  last_pipeline?: {
    id?: string | null;
    status?: string | null;
    updated?: string | null;
  };
  last_sync?: string | null;
};

type ProjectMetaApi = {
  versions: { id: string; label: string }[];
};

type PipelineApi = {
  id: string;
  status: string;
  response_from_ai: any;
  created: string;
  updated: string;
};

type PipelineSummaryApi = {
  id: string;
  status: string;
  project_id: number;
  product_id: number;
  product_name: string;
  started?: string | null;
  created?: string | null;
  updated?: string | null;
  branch?: string | null;
  commit?: string | null;
  findings?: number;
  actions?: Array<{
    source?: string | null;
    type?: string | null;
    status?: string | null;
    updated?: string | null;
  }>;
};

type ListResponse<T> = { results?: T[]; count?: number; next?: string | null; previous?: string | null };

function normalizeTags(raw?: FindingApi["tags"]) {
  if (!raw) return [];
  if (Array.isArray(raw)) {
    return raw
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object" && "name" in item) {
          return String((item as { name?: string }).name ?? "");
        }
        return "";
      })
      .map((tag) => tag.trim())
      .filter(Boolean);
  }
  if (typeof raw === "string") {
    return raw.split(",").map((tag) => tag.trim()).filter(Boolean);
  }
  return [];
}

export function useProjects() {
  return useQuery({
    queryKey: ["aist-projects"],
    queryFn: async () => {
      const payload = await fetchJson<ListResponse<AistProjectApi>>(getRoute("projects_list_url"));
      return normalizeList(payload).map((item): Project => ({
        id: item.id,
        productId: item.product_id,
        name: item.product_name,
      }));
    },
  });
}

export function useProductSummaries() {
  return useQuery({
    queryKey: ["product-summaries"],
    queryFn: async () => {
      const payload = await fetchJson<ListResponse<ProductSummaryApi>>(getRoute("product_summary_url"));
      return normalizeList(payload).map((item): ProductSummary => ({
        projectId: item.project_id,
        productId: item.product_id,
        name: item.product_name,
        tags: item.tags ?? [],
        status: item.status,
        findingsTotal: item.findings_total,
        findingsActive: item.findings_active,
        severity: {
          Critical: item.severity?.Critical ?? 0,
          High: item.severity?.High ?? 0,
          Medium: item.severity?.Medium ?? 0,
          Low: item.severity?.Low ?? 0,
          Info: item.severity?.Info ?? 0,
        },
        risk: {
          riskAccepted: item.risk?.risk_accepted ?? 0,
          underReview: item.risk?.under_review ?? 0,
          mitigated: item.risk?.mitigated ?? 0,
        },
        lastPipeline: item.last_pipeline ?? null,
        lastSync: item.last_sync ?? null,
      }));
    },
  });
}

export function useFindings(productId?: number) {
  const filters: FindingFilters = productId ? { productId } : {};
  return useFindingsWithFilters(filters);
}

export function useFindingsPage(filters: FindingFilters) {
  const limit = filters.limit ?? 50;
  const offset = filters.offset ?? 0;
  return useQuery({
    queryKey: ["findings-page", filters],
    queryFn: async () => {
      const params = new URLSearchParams({
        limit: String(limit),
        offset: String(offset),
        ...(filters.productId ? { test__engagement__product: String(filters.productId) } : {}),
        ...(filters.pipelineId ? { pipeline_id: filters.pipelineId } : {}),
        ...(filters.severity ? { severity: filters.severity } : {}),
        ...(filters.status
          ? { active: filters.status === "enabled" ? "true" : "false" }
          : {}),
        ...(filters.riskStates?.includes("risk_accepted")
          ? { risk_accepted: "true" }
          : {}),
        ...(filters.riskStates?.includes("under_review") ? { under_review: "true" } : {}),
        ...(filters.riskStates?.includes("mitigated") ? { is_mitigated: "true" } : {}),
        ...(filters.cwe ? { cwe: filters.cwe } : {}),
        ...(filters.tags?.length
          ? {
              tags: filters.tags.map((tag) => tag.trim()).filter(Boolean).join(","),
            }
          : {}),
        ...(filters.ordering ? { ordering: filters.ordering } : {}),
      });
      const payload = await fetchJson<ListResponse<FindingApi>>(
        `${getRoute("findings_list_url")}?${params.toString()}`,
      );
      const items = normalizeList(payload).map((item): Finding => ({
        sourceFileLink: item.finding_meta?.find((meta) => meta.name === "sourcefile_link")?.value,
        id: item.id,
        title: item.title,
        severity: item.severity,
        active: item.active,
        isMitigated: item.is_mitigated ?? false,
        riskAccepted: item.risk_accepted ?? false,
        falsePositive: item.false_p ?? false,
        outOfScope: item.out_of_scope ?? false,
        duplicate: item.duplicate ?? false,
        product: String(item.product),
        productId: item.product,
        date: item.date,
        filePath: item.file_path ?? "",
        line: item.line ?? 0,
        tool: "",
        description: item.description ?? undefined,
        cwe: item.cwe ?? null,
        tags: normalizeTags(item.tags),
        testId: item.test ?? null,
        riskStates: [
          item.risk_accepted ? "risk_accepted" : null,
          item.under_review ? "under_review" : null,
          item.is_mitigated ? "mitigated" : null,
        ].filter(Boolean) as Finding["riskStates"],
      }));
      return {
        items,
        count: payload.count ?? items.length,
      };
    },
  });
}

export function useFindingsWithFilters(filters: FindingFilters) {
  const limit = filters.limit ?? 50;
  return useInfiniteQuery({
    queryKey: ["findings", filters],
    queryFn: async ({ pageParam = 0 }) => {
      const params = new URLSearchParams({
        limit: String(limit),
        offset: String(pageParam),
        ...(filters.productId ? { test__engagement__product: String(filters.productId) } : {}),
        ...(filters.pipelineId ? { pipeline_id: filters.pipelineId } : {}),
        ...(filters.severity ? { severity: filters.severity } : {}),
        ...(filters.status
          ? { active: filters.status === "enabled" ? "true" : "false" }
          : {}),
        ...(filters.riskStates?.includes("risk_accepted")
          ? { risk_accepted: "true" }
          : {}),
        ...(filters.riskStates?.includes("under_review") ? { under_review: "true" } : {}),
        ...(filters.riskStates?.includes("mitigated") ? { is_mitigated: "true" } : {}),
        ...(filters.cwe ? { cwe: filters.cwe } : {}),
        ...(filters.tags?.length
          ? {
              tags: filters.tags.map((tag) => tag.trim()).filter(Boolean).join(","),
            }
          : {}),
        ...(filters.ordering ? { ordering: filters.ordering } : {}),
      });
      const payload = await fetchJson<ListResponse<FindingApi>>(
        `${getRoute("findings_list_url")}?${params.toString()}`,
      );
      const items = normalizeList(payload).map((item): Finding => ({
        sourceFileLink: item.finding_meta?.find((meta) => meta.name === "sourcefile_link")?.value,
        id: item.id,
        title: item.title,
        severity: item.severity,
        active: item.active,
        isMitigated: item.is_mitigated ?? false,
        riskAccepted: item.risk_accepted ?? false,
        falsePositive: item.false_p ?? false,
        outOfScope: item.out_of_scope ?? false,
        duplicate: item.duplicate ?? false,
        product: String(item.product),
        productId: item.product,
        date: item.date,
        filePath: item.file_path ?? "",
        line: item.line ?? 0,
        tool: "",
        description: item.description ?? undefined,
        cwe: item.cwe ?? null,
        tags: normalizeTags(item.tags),
        testId: item.test ?? null,
        riskStates: [
          item.risk_accepted ? "risk_accepted" : null,
          item.under_review ? "under_review" : null,
          item.is_mitigated ? "mitigated" : null,
        ].filter(Boolean) as Finding["riskStates"],
      }));
      return {
        items,
        count: payload.count ?? items.length,
        next: payload.next ?? null,
        previous: payload.previous ?? null,
        nextOffset: payload.next ? pageParam + limit : null,
      };
    },
    getNextPageParam: (lastPage) => lastPage.nextOffset ?? undefined,
  });
}

export function useFinding(findingId?: number) {
  return useQuery({
    queryKey: ["finding", findingId],
    queryFn: async () => {
      if (!findingId) return null;
      const item = await fetchJson<FindingApi>(
        getRoute("finding_detail_url", { id: findingId }),
      );
      return {
        id: item.id,
        sourceFileLink: item.finding_meta?.find((meta) => meta.name === "sourcefile_link")?.value,
        title: item.title,
        severity: item.severity,
        active: item.active,
        isMitigated: item.is_mitigated ?? false,
        riskAccepted: item.risk_accepted ?? false,
        falsePositive: item.false_p ?? false,
        outOfScope: item.out_of_scope ?? false,
        duplicate: item.duplicate ?? false,
        product: String(item.product ?? ""),
        productId: item.product,
        date: item.date,
        filePath: item.file_path ?? "",
        line: item.line ?? 0,
        tool: "",
        description: item.description ?? undefined,
        cwe: item.cwe ?? null,
        tags: normalizeTags(item.tags),
        testId: item.test ?? null,
        riskStates: [
          item.risk_accepted ? "risk_accepted" : null,
          item.under_review ? "under_review" : null,
          item.is_mitigated ? "mitigated" : null,
        ].filter(Boolean) as Finding["riskStates"],
      } satisfies Finding;
    },
    enabled: Boolean(findingId),
  });
}

type TestDetailApi = {
  id: number;
  engagement?: number;
  engagement_id?: number;
  product?: number;
  product_id?: number;
};

export function useTestEngagement(testId?: number | null) {
  return useQuery({
    queryKey: ["test", testId],
    queryFn: async () => {
      if (!testId) return null;
      const payload = await fetchJson<TestDetailApi>(getRoute("test_detail_url", { id: testId }));
      return payload.engagement_id ?? payload.engagement ?? null;
    },
    enabled: Boolean(testId),
    staleTime: 10 * 60 * 1000,
  });
}

type EngagementDetailApi = {
  id: number;
  product?: number;
  product_id?: number;
};

export function useEngagementProduct(engagementId?: number | null) {
  return useQuery({
    queryKey: ["engagement", engagementId],
    queryFn: async () => {
      if (!engagementId) return null;
      const payload = await fetchJson<EngagementDetailApi>(
        getRoute("engagement_detail_url", { id: engagementId }),
      );
      return payload.product_id ?? payload.product ?? null;
    },
    enabled: Boolean(engagementId),
    staleTime: 10 * 60 * 1000,
  });
}

type NotesResponse = {
  finding_id: number;
  notes: Note[];
};

export function useFindingNotes(findingId?: number) {
  return useQuery({
    queryKey: ["finding-notes", findingId],
    queryFn: async () => {
      if (!findingId) return [];
      const payload = await fetchJson<NotesResponse>(
        getRoute("finding_notes_url", { id: findingId }),
      );
      return payload.notes ?? [];
    },
    enabled: Boolean(findingId),
  });
}

export function useProjectMeta(projectId?: number) {
  return useQuery({
    queryKey: ["project-meta", projectId],
    queryFn: async () => {
      if (!projectId) return null;
      return fetchJson<ProjectMetaApi>(
        getRoute("project_meta_url", { project_id: projectId }),
      );
    },
    enabled: Boolean(projectId),
  });
}

export function usePipelines(projectId?: number) {
  return useQuery({
    queryKey: ["pipelines", projectId],
    queryFn: async () => {
      if (!projectId) return [];
      const params = new URLSearchParams({
        limit: "5",
        ordering: "-created",
        project_id: String(projectId),
      });
      const payload = await fetchJson<ListResponse<PipelineApi>>(
        `${getRoute("pipelines_list_url")}?${params.toString()}`,
      );
      return normalizeList(payload);
    },
    enabled: Boolean(projectId),
  });
}

type PipelineSummaryFilters = {
  productId?: number;
  status?: string;
  createdGte?: string;
  createdLte?: string;
  search?: string;
  ordering?: string;
  limit?: number;
  offset?: number;
};

export function usePipelineSummaries(filters: PipelineSummaryFilters) {
  return useQuery({
    queryKey: ["pipelines-summary", filters],
    queryFn: async () => {
      const params = new URLSearchParams({
        ...(filters.productId ? { product_id: String(filters.productId) } : {}),
        ...(filters.status ? { status: filters.status } : {}),
        ...(filters.createdGte ? { created_gte: filters.createdGte } : {}),
        ...(filters.createdLte ? { created_lte: filters.createdLte } : {}),
        ...(filters.search ? { search: filters.search } : {}),
        ...(filters.ordering ? { ordering: filters.ordering } : {}),
        limit: String(filters.limit ?? 50),
        offset: String(filters.offset ?? 0),
      });
      const payload = await fetchJson<ListResponse<PipelineSummaryApi>>(
        `${getRoute("pipelines_summary_url")}?${params.toString()}`,
      );
      return {
        items: normalizeList(payload).map((item): PipelineSummary => ({
          id: item.id,
          status: item.status,
          projectId: item.project_id,
          productId: item.product_id,
          productName: item.product_name,
          started: item.started ?? null,
          created: item.created ?? null,
          updated: item.updated ?? null,
          branch: item.branch ?? null,
          commit: item.commit ?? null,
          findings: item.findings ?? 0,
          actions: item.actions ?? [],
        })),
        count: payload.count ?? 0,
      };
    },
  });
}

export function useFindingTags() {
  return useQuery({
    queryKey: ["finding-tags", "all"],
    queryFn: async () => {
      const payload = await fetchJson<{ tags: string[] }>(getRoute("finding_tags_url"));
      const cleaned = (payload.tags ?? []).map((tag) => tag.trim()).filter(Boolean);
      return Array.from(new Set(cleaned));
    },
    staleTime: 5 * 60 * 1000,
  });
}

export function useFindingTagsByProduct(productId?: number) {
  return useQuery({
    queryKey: ["finding-tags", productId ?? "all"],
    queryFn: async () => {
      const params = productId ? `?product_id=${productId}` : "";
      const payload = await fetchJson<{ tags: string[] }>(`${getRoute("finding_tags_url")}${params}`);
      const cleaned = (payload.tags ?? []).map((tag) => tag.trim()).filter(Boolean);
      return Array.from(new Set(cleaned));
    },
    staleTime: 5 * 60 * 1000,
  });
}

function findAiEntry(response: any, findingId: number) {
  if (!response?.results) return null;
  const { true_positives, false_positives, uncertainly } = response.results;
  const pools = [
    ...(true_positives ?? []),
    ...(false_positives ?? []),
    ...(uncertainly ?? []),
  ];
  return pools.find((entry: any) => entry?.originalFinding?.id === findingId);
}

export function useAiResponse(pipelines: PipelineApi[], findingId?: number): AIResponse | null {
  if (!findingId) return null;
  for (const pipeline of pipelines) {
    const entry = findAiEntry(pipeline.response_from_ai, findingId);
    if (entry) {
      return {
        reasoning: entry.reasoning ?? "AI response available.",
        epssScore: entry.epssScore,
        impactScore: entry.impactScore,
        exploitabilityScore: entry.exploitabilityScore,
        references: entry.references,
        pipelineId: pipeline.id,
      };
    }
  }
  return null;
}
