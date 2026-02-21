import SelectField from "./SelectField";
import DateField from "./DateField";
import TextInput from "./TextInput";

type Option = {
  value: string;
  label: string;
};

type PipelineFilterPanelProps = {
  projectOptions: Option[];
  selectedProjectId?: number;
  onProjectChange: (value?: number) => void;
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
  projectOptions,
  selectedProjectId,
  onProjectChange,
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
    <aside className="p-5 aist-card">
      <div className="text-xs uppercase tracking-[0.2em] text-slate-400 mb-4">Filters</div>
      <div className="space-y-4">
        <SelectField
          label="Project"
          value={selectedProjectId ? String(selectedProjectId) : ""}
          onChange={(value) => onProjectChange(value ? Number(value) : undefined)}
          placeholder="All projects"
          options={projectOptions}
        />
        <SelectField label="Status" value={status} onChange={onStatusChange} options={statusOptions} />
        <div>
          <label className="text-xs text-slate-400">Branch / Commit</label>
          <TextInput
            className="mt-2 px-4"
            placeholder="Search branch or commit..."
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
          />
        </div>
        <div>
          <label className="text-xs text-slate-400">Created between</label>
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
      </div>
    </aside>
  );
}
