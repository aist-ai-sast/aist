import TextInput from "./TextInput";
import PasswordField from "./PasswordField";

/** A variable row of the form; ``stored`` rows keep their saved value while ``value`` is blank. */
export type BuildEnvRow = {
  key: number;
  name: string;
  value: string;
  stored: boolean;
};

let nextBuildEnvRowKey = 0;

/** A form row with a key no other row of this page ever had. */
export function buildEnvRow(row: Omit<BuildEnvRow, "key">): BuildEnvRow {
  nextBuildEnvRowKey += 1;
  return { key: nextBuildEnvRowKey, ...row };
}

/** A row the backend would refuse: no name, or a new name without a value. */
export function isIncompleteBuildEnvRow(row: BuildEnvRow): boolean {
  return !row.name.trim() || (!row.stored && !row.value);
}

export default function BuildEnvFields({ rows, onChange }: { rows: BuildEnvRow[]; onChange: (rows: BuildEnvRow[]) => void }) {
  function update(index: number, patch: Partial<BuildEnvRow>) {
    onChange(rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  }
  return (
    <div className="space-y-2 sm:col-span-2">
      <div className="text-xs text-slate-400">Variables</div>
      {rows.map((row, index) => (
        <div key={row.key} className="flex flex-wrap items-center gap-2">
          <TextInput
            className="min-w-0 flex-1 basis-40 font-mono"
            aria-label="Variable name"
            placeholder="CONAN_PASSWORD"
            value={row.name}
            disabled={row.stored}
            onChange={(e) => update(index, { name: e.target.value })}
          />
          <PasswordField
            className="min-w-0 flex-1 basis-40"
            placeholder={row.stored ? "stored — leave blank to keep" : "value"}
            value={row.value}
            onChange={(e) => update(index, { value: e.target.value })}
            autoComplete="new-password"
          />
          <button
            type="button"
            className="shrink-0 text-[11px] text-slate-400 transition hover:text-danger-400"
            onClick={() => onChange(rows.filter((_, i) => i !== index))}
            title="Remove variable"
            aria-label={`Remove ${row.name || "variable"}`}
          >
            ✕
          </button>
        </div>
      ))}
      <button
        type="button"
        className="aist-icon-button border-night-400/60 bg-night-700/60 px-2.5 py-1.5 text-[11px] text-slate-300"
        onClick={() => onChange([...rows, buildEnvRow({ name: "", value: "", stored: false })])}
      >
        + Add variable
      </button>
      <p className="text-[11px] text-slate-500">
        Passed to the project builder as environment variables. Every value is treated as a secret: it is
        encrypted, masked in pipeline logs and never shown again.
      </p>
    </div>
  );
}
