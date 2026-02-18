import type { Project } from "../types";
import MultiSelectChips from "./MultiSelectChips";
import SelectField from "./SelectField";

type FilterPanelProps = {
  products: Project[];
  selectedProductId?: number;
  onProductChange: (productId?: number) => void;
  selectedSeverities: string[];
  onSeveritiesChange: (value: string[]) => void;
  selectedFile: string;
  onFileChange: (value: string) => void;
  selectedProjectVersion: string;
  onProjectVersionChange: (value: string) => void;
  selectedStatus: string;
  onStatusChange: (value: string) => void;
  selectedRisk: string[];
  onRiskChange: (value: string[]) => void;
  selectedCwe: string;
  onCweChange: (value: string) => void;
  availableTags: string[];
  selectedTags: string[];
  onTagsChange: (value: string[]) => void;
  selectedAiResponse: string;
  onAiResponseChange: (value: string) => void;
};

export default function FilterPanel({
  products,
  selectedProductId,
  onProductChange,
  selectedSeverities,
  onSeveritiesChange,
  selectedFile,
  onFileChange,
  selectedProjectVersion,
  onProjectVersionChange,
  selectedStatus,
  onStatusChange,
  selectedRisk,
  onRiskChange,
  selectedCwe,
  onCweChange,
  availableTags,
  selectedTags,
  onTagsChange,
  selectedAiResponse,
  onAiResponseChange,
}: FilterPanelProps) {
  return (
    <aside className="p-5 aist-card aist-filter-panel overflow-hidden">
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
        <MultiSelectChips
          label="Severity"
          options={["Critical", "High", "Medium", "Low", "Info"]}
          selected={selectedSeverities}
          onChange={onSeveritiesChange}
        />
        <div>
          <label className="text-xs text-slate-400">Project Version</label>
          <input
            className="mt-2 w-full rounded-xl border border-night-500 bg-night-600 px-3 py-2 text-sm text-white placeholder:text-slate-400"
            value={selectedProjectVersion}
            onChange={(event) => onProjectVersionChange(event.target.value)}
            placeholder="e.g. master or commit hash"
          />
        </div>
        <div>
          <label className="text-xs text-slate-400">File</label>
          <input
            className="mt-2 w-full rounded-xl border border-night-500 bg-night-600 px-3 py-2 text-sm text-white placeholder:text-slate-400"
            value={selectedFile}
            onChange={(event) => onFileChange(event.target.value)}
            placeholder="e.g. src/app/main.py"
          />
        </div>
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
          label="AI Response"
          value={selectedAiResponse}
          onChange={onAiResponseChange}
          options={[
            { value: "All", label: "All" },
            { value: "has_ai", label: "Has AI Response" },
            { value: "no_ai", label: "No AI Response" },
          ]}
        />
      </div>
    </aside>
  );
}
