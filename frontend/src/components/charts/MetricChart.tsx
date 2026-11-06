import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { MetricRow } from "@/api/types";
import { SERIES_COLORS } from "@/lib/utils";

interface SeriesDef {
  key: keyof MetricRow;
  label: string;
  unit?: string;
}

export function MetricChart({
  points,
  series,
  height = 220,
  domain,
}: {
  points: MetricRow[];
  series: SeriesDef[];
  height?: number;
  domain?: [number, number];
}) {
  const data = points.map((p) => ({
    ...p,
    label: new Date(p.ts).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
    }),
  }));

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -18 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
        <XAxis
          dataKey="label"
          tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
          tickLine={false}
          axisLine={false}
          minTickGap={48}
        />
        <YAxis
          tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
          tickLine={false}
          axisLine={false}
          domain={domain ?? ["auto", "auto"]}
          width={48}
        />
        <Tooltip
          contentStyle={{
            background: "hsl(var(--card))",
            border: "1px solid hsl(var(--border))",
            borderRadius: 8,
            fontSize: 12,
          }}
          formatter={(value: number, name: string) => [
            typeof value === "number" ? value.toFixed(1) : value,
            name,
          ]}
        />
        {series.map((def, index) => (
          <Line
            key={String(def.key)}
            type="monotone"
            dataKey={String(def.key)}
            name={def.label}
            stroke={SERIES_COLORS[index % SERIES_COLORS.length]}
            strokeWidth={1.8}
            dot={false}
            connectNulls
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
