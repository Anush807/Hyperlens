import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { Forecast } from "@/api/types";

/**
 * LSTM and seasonal-naive forecasts on one axis, with the LSTM's residual band
 * shaded and the capacity threshold drawn in. Showing both models is the point:
 * the baseline is how you judge whether the LSTM earned its keep.
 */
export function ForecastChart({
  forecasts,
  threshold,
  preferredModel = "naive",
  height = 260,
}: {
  forecasts: Forecast[];
  threshold: number;
  /** The model that won the holdout. It is drawn solid; the other is dashed. */
  preferredModel?: "lstm" | "naive";
  height?: number;
}) {
  const lstm = forecasts.find((f) => f.model === "lstm");
  const naive = forecasts.find((f) => f.model === "naive");
  const reference = lstm ?? naive;
  if (!reference) return null;
  const lstmPreferred = preferredModel === "lstm";

  const data = reference.points.map((point, index) => {
    const lstmPoint = lstm?.points[index];
    const naivePoint = naive?.points[index];
    return {
      date: point.date,
      label: new Date(point.date).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
      }),
      lstm: lstmPoint?.value ?? null,
      naive: naivePoint?.value ?? null,
      // Recharts stacks the band as [lower, height], not [lower, upper].
      bandBase: lstmPoint?.lower ?? null,
      bandSpan:
        lstmPoint && lstmPoint.upper > lstmPoint.lower ? lstmPoint.upper - lstmPoint.lower : null,
    };
  });

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: -18 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
        <XAxis
          dataKey="label"
          tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
          tickLine={false}
          axisLine={false}
          minTickGap={36}
        />
        <YAxis
          tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }}
          tickLine={false}
          axisLine={false}
          domain={[0, 100]}
          width={48}
        />
        <Tooltip
          contentStyle={{
            background: "hsl(var(--card))",
            border: "1px solid hsl(var(--border))",
            borderRadius: 8,
            fontSize: 12,
          }}
          formatter={(value: number, name: string) =>
            name === "bandBase" || name === "bandSpan"
              ? [null, null]
              : [typeof value === "number" ? `${value.toFixed(1)}%` : value, name]
          }
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Area
          dataKey="bandBase"
          stackId="band"
          stroke="none"
          fill="transparent"
          legendType="none"
          isAnimationActive={false}
        />
        <Area
          dataKey="bandSpan"
          stackId="band"
          stroke="none"
          fill="hsl(217 91% 60%)"
          fillOpacity={0.14}
          name="LSTM 10-90% band"
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="lstm"
          name={lstmPreferred ? "LSTM (default)" : "LSTM"}
          stroke="hsl(217 91% 60%)"
          strokeWidth={lstmPreferred ? 2.2 : 1.5}
          strokeDasharray={lstmPreferred ? undefined : "4 3"}
          dot={false}
          connectNulls
          isAnimationActive={false}
        />
        <Line
          type="monotone"
          dataKey="naive"
          name={lstmPreferred ? "Seasonal naive" : "Seasonal naive (default)"}
          stroke="hsl(174 72% 45%)"
          strokeWidth={lstmPreferred ? 1.5 : 2.2}
          strokeDasharray={lstmPreferred ? "4 3" : undefined}
          dot={false}
          connectNulls
          isAnimationActive={false}
        />
        <ReferenceLine
          y={threshold}
          stroke="hsl(var(--destructive))"
          strokeDasharray="5 4"
          label={{
            value: `${threshold}% threshold`,
            position: "insideTopRight",
            fill: "hsl(var(--destructive))",
            fontSize: 10,
          }}
        />
      </ComposedChart>
    </ResponsiveContainer>
  );
}
