import type { Project } from "../types";
import SelectField from "./SelectField";
import MultiSelectChips from "./MultiSelectChips";

type FilterPanelProps = {
  products: Project[];
  selectedProductId?: number;
  onProductChange: (productId?: number) => void;
  selectedSeverity: string;
  onSeverityChange: (value: string) => void;
  selectedStatus: string;
  onStatusChange: (value: string) => void;
  selectedRisk: string[];
  onRiskChange: (value: string[]) => void;
  selectedCwe: string;
  onCweChange: (value: string) => void;
  availableTags: string[];
  selectedTags: string[];
  onTagsChange: (value: string[]) => void;
  selectedAiVerdict: string;
  onAiVerdictChange: (value: string) => void;
  aiVerdictDisabled?: boolean;
};

export default function FilterPanel({
  products,
  selectedProductId,
  onProductChange,
  selectedSeverity,
  onSeverityChange,
  selectedStatus,
  onStatusChange,
  selectedRisk,
  onRiskChange,
  selectedCwe,
  onCweChange,
  availableTags,
  selectedTags,
  onTagsChange,
  selectedAiVerdict,
  onAiVerdictChange,
  aiVerdictDisabled,
}: FilterPanelProps) {
  return (
    <aside className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel">
      <div className="text-xs uppercase tracking-[0.2em] text-slate-400 mb-4">
        Filters
      </div>
      <div className="space-y-4">
        <SelectField
          label="Product"
          value={selectedProductId ? String(selectedProductId) : ""}
          onChange={(value) => onProductChange(value ? Number(value) : undefined)}
          placeholder="All products"
          options={[
            ...products.map((product) => ({
              value: String(product.productId),
              label: product.name,
            })),
          ]}
        />
        <SelectField
          label="Severity"
          value={selectedSeverity}
          onChange={onSeverityChange}
          options={["All severities", "Critical", "High", "Medium", "Low", "Info"].map((option) => ({
            value: option,
            label: option,
          }))}
        />
        <SelectField
          label="Status"
          value={selectedStatus}
          onChange={onStatusChange}
          options={["All", "Active", "Non-Active"].map((option) => ({
            value: option,
            label: option,
          }))}
        />
        <MultiSelectChips
          label="Risk State"
          options={["Risk Accepted", "Under Review", "Mitigated"]}
          selected={[
            selectedRisk.includes("risk_accepted") ? "Risk Accepted" : "",
            selectedRisk.includes("under_review") ? "Under Review" : "",
            selectedRisk.includes("mitigated") ? "Mitigated" : "",
          ].filter(Boolean)}
          onChange={(values) => {
            const next: string[] = [];
            if (values.includes("Risk Accepted")) next.push("risk_accepted");
            if (values.includes("Under Review")) next.push("under_review");
            if (values.includes("Mitigated")) next.push("mitigated");
            onRiskChange(next);
          }}
        />
        <div>
          <label className="text-xs text-slate-400">CWE (comma-separated)</label>
          <input
            className="mt-2 w-full rounded-xl border border-night-500 bg-night-600 px-3 py-2 text-sm text-white placeholder:text-slate-400"
            value={selectedCwe}
            onChange={(event) => onCweChange(event.target.value)}
            placeholder="e.g. 79, 89"
          />
        </div>
        <MultiSelectChips
          label="Tags"
          options={availableTags}
          selected={selectedTags}
          onChange={onTagsChange}
          emptyLabel="No tags available."
          visibleCount={10}
        />
        <SelectField
          label="AI Verdict"
          value={selectedAiVerdict}
          onChange={onAiVerdictChange}
          disabled={aiVerdictDisabled}
          options={[
            { value: "All", label: "All" },
            { value: "true_positive", label: "True Positive" },
            { value: "false_positive", label: "False Positive" },
            { value: "uncertain", label: "Uncertain" },
          ]}
        />
        {aiVerdictDisabled ? (
          <p className="text-xs text-slate-500">
            Select a product to enable AI verdict filters.
          </p>
        ) : null}
      </div>
    </aside>
  );
}
