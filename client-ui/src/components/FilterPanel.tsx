import type { Project } from "../types";
import DateField from "./DateField";
import FilterClearButton from "./FilterClearButton";
import MultiSelectChips from "./MultiSelectChips";
import SelectField from "./SelectField";
import TextInput from "./TextInput";

type FilterPanelProps = {
  products: Project[];
  selectedProjectId?: number;
  onProjectChange: (projectId?: number) => void;
  selectedSeverities: string[];
  onSeveritiesChange: (value: string[]) => void;
  selectedFile: string;
  onFileChange: (value: string) => void;
  createdFrom: string;
  onCreatedFromChange: (value: string) => void;
  createdTo: string;
  onCreatedToChange: (value: string) => void;
  selectedProjectVersion: string;
  onProjectVersionChange: (value: string) => void;
  selectedTitle: string;
  onTitleChange: (value: string) => void;
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
  selectedHasWorkItem: string;
  onHasWorkItemChange: (value: string) => void;
  onClearAll: () => void;
};

export default function FilterPanel({
  products,
  selectedProjectId,
  onProjectChange,
  selectedSeverities,
  onSeveritiesChange,
  selectedFile,
  onFileChange,
  createdFrom,
  onCreatedFromChange,
  createdTo,
  onCreatedToChange,
  selectedProjectVersion,
  onProjectVersionChange,
  selectedTitle,
  onTitleChange,
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
  selectedHasWorkItem,
  onHasWorkItemChange,
  onClearAll,
}: FilterPanelProps) {
  return (
    <aside className="p-5 aist-card aist-filter-panel overflow-hidden">
      <div className="mb-4 flex items-center justify-between gap-2">
        <div className="text-xs uppercase tracking-[0.2em] text-slate-400">
          Filters
        </div>
        <FilterClearButton onClick={onClearAll} label="Clear all" />
      </div>
      <div className="space-y-4">
        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <label className="text-xs text-slate-400">Project</label>
            {selectedProjectId ? (
              <FilterClearButton onClick={() => onProjectChange(undefined)} />
            ) : null}
          </div>
          <SelectField
            label="Project"
            hideLabel
            value={selectedProjectId ? String(selectedProjectId) : "all"}
            onChange={(value) => onProjectChange(value && value !== "all" ? Number(value) : undefined)}
            placeholder="All projects"
            options={[
              { value: "all", label: "All projects" },
              ...products.map((product) => ({
                value: String(product.id),
                label: product.name,
              })),
            ]}
          />
        </div>
        <MultiSelectChips
          label="Severity"
          options={["Critical", "High", "Medium", "Low", "Info"]}
          selected={selectedSeverities}
          onChange={onSeveritiesChange}
          onClear={() => onSeveritiesChange([])}
        />
        <div>
          <div className="flex items-center justify-between gap-2">
            <label className="text-xs text-slate-400">Project Version</label>
            {selectedProjectVersion ? (
              <FilterClearButton onClick={() => onProjectVersionChange("")} />
            ) : null}
          </div>
          <TextInput
            className="mt-2"
            value={selectedProjectVersion}
            onChange={(event) => onProjectVersionChange(event.target.value)}
            placeholder="e.g. master or commit hash"
          />
        </div>
        <div>
          <div className="flex items-center justify-between gap-2">
            <label className="text-xs text-slate-400">File</label>
            {selectedFile ? (
              <FilterClearButton onClick={() => onFileChange("")} />
            ) : null}
          </div>
          <TextInput
            className="mt-2"
            value={selectedFile}
            onChange={(event) => onFileChange(event.target.value)}
            placeholder="e.g. src/app/main.py"
          />
        </div>
        <div>
          <div className="flex items-center justify-between gap-2">
            <label className="text-xs text-slate-400">Title</label>
            {selectedTitle ? (
              <FilterClearButton onClick={() => onTitleChange("")} />
            ) : null}
          </div>
          <TextInput
            className="mt-2"
            value={selectedTitle}
            onChange={(event) => onTitleChange(event.target.value)}
            placeholder="e.g. SQL Injection"
          />
        </div>
        <div>
          <div className="flex items-center justify-between gap-2">
            <label className="text-xs text-slate-400">Created between</label>
            {createdFrom || createdTo ? (
              <FilterClearButton
                onClick={() => {
                  onCreatedFromChange("");
                  onCreatedToChange("");
                }}
              />
            ) : null}
          </div>
          <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
            <DateField
              label="From"
              value={createdFrom}
              onChange={onCreatedFromChange}
              placeholder="dd.mm.yy"
            />
            <DateField
              label="To"
              value={createdTo}
              onChange={onCreatedToChange}
              placeholder="dd.mm.yy"
            />
          </div>
        </div>
        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <label className="text-xs text-slate-400">Status</label>
            {selectedStatus !== "All" ? (
              <FilterClearButton onClick={() => onStatusChange("All")} />
            ) : null}
          </div>
          <SelectField
            label="Status"
            hideLabel
            value={selectedStatus}
            onChange={onStatusChange}
            options={["All", "Active", "Non-Active"].map((option) => ({
              value: option,
              label: option,
            }))}
          />
        </div>
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
          onClear={() => onRiskChange([])}
        />
        <div>
          <div className="flex items-center justify-between gap-2">
            <label className="text-xs text-slate-400">CWE (comma-separated)</label>
            {selectedCwe ? (
              <FilterClearButton onClick={() => onCweChange("")} />
            ) : null}
          </div>
          <TextInput
            className="mt-2"
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
          onClear={() => onTagsChange([])}
          emptyLabel="No tags available."
          visibleCount={10}
        />
        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <label className="text-xs text-slate-400">AI Status</label>
            {selectedAiResponse !== "All" ? (
              <FilterClearButton onClick={() => onAiResponseChange("All")} />
            ) : null}
          </div>
          <SelectField
            label="AI Status"
            hideLabel
            value={selectedAiResponse}
            onChange={onAiResponseChange}
            options={[
              { value: "All", label: "All" },
              { value: "has_ai", label: "Has AI Response" },
              { value: "no_ai", label: "No AI Response" },
              { value: "ai_tp", label: "AI TP" },
              { value: "ai_fp", label: "AI FP" },
              { value: "ai_u", label: "AI U" },
            ]}
          />
        </div>
        <div>
          <div className="mb-1 flex items-center justify-between gap-2">
            <label className="text-xs text-slate-400">Work Item</label>
            {selectedHasWorkItem !== "all" ? (
              <FilterClearButton onClick={() => onHasWorkItemChange("all")} />
            ) : null}
          </div>
          <SelectField
            label="Work Item"
            hideLabel
            value={selectedHasWorkItem}
            onChange={onHasWorkItemChange}
            options={[
              { value: "all", label: "All" },
              { value: "yes", label: "Has work item" },
              { value: "no", label: "No work item" },
            ]}
          />
        </div>
      </div>
    </aside>
  );
}
