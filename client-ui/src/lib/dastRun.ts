/**
 * Display formatting for the DAST run metadata a report carries.
 *
 * Every value is optional at the source, so each helper returns null for "the report did not
 * report it" and the caller drops the row rather than showing a zero or a dash.
 */

import type { DastTokenBucket } from "../types";

const NUMBER = new Intl.NumberFormat("en-US");

export function formatCount(value: number | null | undefined): string | null {
  return value === null || value === undefined ? null : NUMBER.format(value);
}

/** Token counts run to tens of millions, so the headline is compact and the detail exact. */
export function formatCompactTokens(value: number | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return NUMBER.format(value);
}

export function formatDuration(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined) return null;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  const parts = [];
  if (hours) parts.push(`${hours}h`);
  if (hours || minutes) parts.push(`${minutes}m`);
  parts.push(`${rest}s`);
  return parts.join(" ");
}

/**
 * Phase buckets are keyed by number and only some carry a name — an unnamed phase reads
 * "Phase 4" rather than showing a blank label. Agent buckets are keyed by their own name.
 */
export function bucketLabel(bucket: DastTokenBucket, kind: "phase" | "agent"): string {
  if (kind === "agent") return bucket.key;
  if (!bucket.name) return `Phase ${bucket.key}`;
  if (bucket.name === bucket.key) return bucket.key;
  return /^\d+$/.test(bucket.key) ? `Phase ${bucket.key} · ${bucket.name}` : bucket.name;
}

/** Share of the run total, as a percentage, or null when either side went unreported. */
export function bucketShare(bucket: DastTokenBucket, total: number | null): number | null {
  if (bucket.total_tokens === null || bucket.total_tokens === undefined) return null;
  if (total === null || total === 0) return null;
  return (bucket.total_tokens / total) * 100;
}

export function formatShare(share: number | null): string | null {
  return share === null ? null : `${share.toFixed(1)}%`;
}

/** Buckets ordered by spend, so the panel opens on where the run actually went. */
export function bucketsBySpend(buckets: DastTokenBucket[] | null): DastTokenBucket[] {
  if (!buckets) return [];
  return [...buckets].sort((a, b) => (b.total_tokens ?? 0) - (a.total_tokens ?? 0));
}
