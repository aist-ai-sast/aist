import SelectField from "./SelectField";

type Option = {
  value: string;
  label: string;
};

type PipelineFilterPanelProps = {
  productOptions: Option[];
  selectedProductId?: number;
  onProductChange: (value?: number) => void;
  status: string;
  onStatusChange: (value: string) => void;
  search: string;
  onSearchChange: (value: string) => void;
  createdFrom: string;
  onCreatedFromChange: (value: string) => void;
  createdTo: string;
  onCreatedToChange: (value: string) => void;
  statusOptions: Option[];
};

export default function PipelineFilterPanel({
  productOptions,
  selectedProductId,
  onProductChange,
  status,
  onStatusChange,
  search,
  onSearchChange,
  createdFrom,
  onCreatedFromChange,
  createdTo,
  onCreatedToChange,
  statusOptions,
}: PipelineFilterPanelProps) {
  return (
    <aside className="rounded-2xl border border-night-500 bg-night-700 p-5 shadow-panel">
      <div className="text-xs uppercase tracking-[0.2em] text-slate-400 mb-4">Filters</div>
      <div className="space-y-4">
        <SelectField
          label="Product"
          value={selectedProductId ? String(selectedProductId) : ""}
          onChange={(value) => onProductChange(value ? Number(value) : undefined)}
          placeholder="All products"
          options={productOptions}
        />
        <SelectField label="Status" value={status} onChange={onStatusChange} options={statusOptions} />
        <div>
          <label className="text-xs text-slate-400">Branch / Commit</label>
          <input
            className="mt-2 h-10 w-full rounded-xl border border-night-500 bg-night-600 px-4 text-sm text-white placeholder:text-slate-400 outline-none focus-visible:border-brand-600 focus-visible:ring-2 focus-visible:ring-brand-600/60"
            placeholder="Search branch or commit..."
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
          />
        </div>
        <div>
          <label className="text-xs text-slate-400">Created between</label>
          <div className="mt-2 grid grid-cols-2 gap-2">
            <input
              type="date"
              className="date-input h-10 w-full rounded-xl border border-night-500 bg-night-600 px-3 text-sm text-white outline-none focus-visible:border-brand-600 focus-visible:ring-2 focus-visible:ring-brand-600/60"
              value={createdFrom}
              onChange={(event) => onCreatedFromChange(event.target.value)}
            />
            <input
              type="date"
              className="date-input h-10 w-full rounded-xl border border-night-500 bg-night-600 px-3 text-sm text-white outline-none focus-visible:border-brand-600 focus-visible:ring-2 focus-visible:ring-brand-600/60"
              value={createdTo}
              onChange={(event) => onCreatedToChange(event.target.value)}
            />
          </div>
        </div>
      </div>
    </aside>
  );
}
