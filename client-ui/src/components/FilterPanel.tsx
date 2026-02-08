import type { Project } from "../types";
import SelectField from "./SelectField";

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
          options={["All", "Enabled", "Disabled"].map((option) => ({
            value: option,
            label: option,
          }))}
        />
        <div>
          <label className="text-xs text-slate-400">Risk State</label>
          <div className="mt-2 grid gap-2 text-xs text-slate-200">
            {["risk_accepted", "under_review", "mitigated"].map((risk) => (
              <label key={risk} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={selectedRisk.includes(risk)}
                  onChange={(event) => {
                    if (event.target.checked) {
                      onRiskChange([...selectedRisk, risk]);
                    } else {
                      onRiskChange(selectedRisk.filter((item) => item !== risk));
                    }
                  }}
                />
                {risk === "risk_accepted"
                  ? "Risk Accepted"
                  : risk === "under_review"
                    ? "Under Review"
                    : "Mitigated"}
              </label>
            ))}
          </div>
        </div>
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
