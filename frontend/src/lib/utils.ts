import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatGb(mb: number | null | undefined): string {
  if (mb === null || mb === undefined) return "—";
  return `${(mb / 1024).toFixed(1)} GB`;
}

export function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(1)}%`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function relativeTime(value: string | null | undefined): string {
  if (!value) return "never";
  const deltaSeconds = (Date.now() - new Date(value).getTime()) / 1000;
  const units: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, "second"],
    [3600, "minute"],
    [86400, "hour"],
    [2592000, "day"],
  ];
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  let previous = 1;
  for (const [limit, unit] of units) {
    if (deltaSeconds < limit) return formatter.format(-Math.round(deltaSeconds / previous), unit);
    previous = limit;
  }
  return formatter.format(-Math.round(deltaSeconds / 2592000), "month");
}

/** Shared palette so every chart in the app reads as one system. */
export const SERIES_COLORS = [
  "hsl(217 91% 60%)",
  "hsl(174 72% 45%)",
  "hsl(38 92% 55%)",
  "hsl(280 65% 65%)",
  "hsl(0 72% 60%)",
] as const;

/**
 * Read one number out of a JSONB blob the API typed as `Record<string, never>`.
 *
 * `anomalies.features` and `recommendations.evidence` are free-form by design -
 * the feature set changes when the model does - so the generated type cannot
 * promise which keys exist or that they are numeric. This narrows at the point
 * of use instead of lying about the shape.
 */
export function numericField(
  blob: Record<string, unknown> | null | undefined,
  key: string,
): number | null {
  const value = blob?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function textField(
  blob: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  const value = blob?.[key];
  return typeof value === "string" ? value : null;
}
