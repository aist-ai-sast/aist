const DATE_ONLY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const HAS_TIME_PATTERN = /[tT]\d{2}:\d{2}/;

export function formatDateForUI(value?: string | null): string | null {
  if (!value) return null;

  const normalized = value.trim();
  if (!normalized) return null;

  if (DATE_ONLY_PATTERN.test(normalized)) {
    return normalized;
  }

  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    return normalized;
  }

  if (!HAS_TIME_PATTERN.test(normalized)) {
    return date.toLocaleDateString();
  }

  return date.toLocaleString();
}
