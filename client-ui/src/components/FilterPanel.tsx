import type { Project } from "../types";

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
        <div>
          <label className="text-xs text-slate-400">Product</label>
          <select
            className="mt-2 w-full rounded-xl border border-night-500 bg-night-600 px-3 py-2 text-sm text-white"
            value={selectedProductId ?? ""}
            onChange={(event) =>
              onProductChange(event.target.value ? Number(event.target.value) : undefined)
            }
          >
            <option value="">All products</option>
            {products.map((product) => (
              <option key={product.id} value={product.productId}>
                {product.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-400">Severity</label>
          <select
            className="mt-2 w-full rounded-xl border border-night-500 bg-night-600 px-3 py-2 text-sm text-white"
            value={selectedSeverity}
            onChange={(event) => onSeverityChange(event.target.value)}
          >
            {["All severities", "Critical", "High", "Medium", "Low", "Info"].map((option) => (
              <option key={option}>{option}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-xs text-slate-400">Status</label>
          <select
            className="mt-2 w-full rounded-xl border border-night-500 bg-night-600 px-3 py-2 text-sm text-white"
            value={selectedStatus}
            onChange={(event) => onStatusChange(event.target.value)}
          >
            {["All", "Enabled", "Disabled"].map((option) => (
              <option key={option}>{option}</option>
            ))}
          </select>
        </div>
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
        <div>
          <label className="text-xs text-slate-400">AI Verdict</label>
          <select
            className="mt-2 w-full rounded-xl border border-night-500 bg-night-600 px-3 py-2 text-sm text-white"
            value={selectedAiVerdict}
            onChange={(event) => onAiVerdictChange(event.target.value)}
            disabled={aiVerdictDisabled}
          >
            <option value="All">All</option>
            <option value="true_positive">True Positive</option>
            <option value="false_positive">False Positive</option>
            <option value="uncertain">Uncertain</option>
          </select>
          {aiVerdictDisabled ? (
            <p className="mt-2 text-xs text-slate-500">
              Select a product to enable AI verdict filters.
            </p>
          ) : null}
        </div>
      </div>
    </aside>
  );
}
