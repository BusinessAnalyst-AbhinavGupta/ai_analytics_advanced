'use client';

import { ChartRenderer, type ChartConfig } from '@/components/ChartRenderer';
import type { ChartSpec } from '@/types/analysis';

/**
 * The chart the Python step already specified.
 *
 * Plan A's sandbox emits a neutral {kind, x, y, series, title}; ChartRenderer
 * wants recharts' {type, xKey, series[]}. The mapping is spelled out because a
 * wrong axis is a silent lie -- it renders perfectly and says something untrue.
 */

const KINDS: Record<string, ChartConfig['type']> = {
  bar: 'BarChart',
  line: 'LineChart',
  area: 'AreaChart',
  scatter: 'ScatterChart',
};

/**
 * Pure, and exported separately so it is testable without recharts: in jsdom a
 * ResponsiveContainer measures a zero-size parent and renders nothing at all.
 */
export function specToConfig(spec: ChartSpec | null | undefined): ChartConfig | null {
  if (!spec) return null;

  const type = KINDS[String(spec.kind ?? '').toLowerCase()];
  // An unknown kind renders nothing rather than guessing a chart type. A wrong
  // chart is worse than no chart.
  if (!type) return null;

  const xKey = spec.x;
  if (!xKey) return null;

  const keys = (Array.isArray(spec.y) ? spec.y : [spec.y]).filter(Boolean);
  if (!keys.length) return null;

  return { type, xKey, series: keys.map((key) => ({ key, name: key })) };
}

export function AnalysisChart({
  spec, data,
}: {
  spec?: ChartSpec | null;
  data?: unknown[];
}) {
  const config = specToConfig(spec);
  if (!config || !data?.length) return null;

  return (
    <figure style={{ margin: '1.5rem 0 0', padding: '1rem', background: 'rgba(0,0,0,0.1)', borderRadius: '8px' }}>
      <ChartRenderer data={data as Record<string, unknown>[]} config={config} />
      {(spec?.title || spec?.series) && (
        <figcaption style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginTop: '0.5rem' }}>
          {spec?.title}
          {/* `series` is a GROUPING column, which is not a recharts concept. It
              must not be silently dropped -- the reader needs to know the data
              is grouped -- but pivoting in the client is out of scope. */}
          {spec?.series ? `${spec?.title ? ' · ' : ''}grouped by ${spec.series}` : ''}
        </figcaption>
      )}
    </figure>
  );
}

/** Is this already a recharts config rather than a neutral spec? */
export function asChartConfig(value: unknown): ChartConfig | null {
  const c = value as Partial<ChartConfig> | undefined;
  if (c?.type && c?.xKey && Array.isArray(c.series) && c.series.length) {
    return c as ChartConfig;
  }
  return null;
}

/**
 * Chart for one turn.
 *
 * Discrimination is on *shape*, never on which field happens to be populated,
 * because neither field is one thing. stakeholder.py sets
 * `artifact.chart_spec = result.chart_spec or chart_config`: the sandbox emits
 * the neutral {kind, x, y} spec, but when it emits none the synthesis step's
 * recharts-shaped {type, xKey, series} lands in the same field. Then
 * `out["chart_config"] = artifact.chart_spec`, so both fields can carry either
 * shape. Real turns in this database do exactly that.
 *
 * So each candidate is tested against both shapes, in order of preference, and
 * the first that yields a renderable config wins.
 */
export function MessageChart({
  spec, legacyConfig, data,
}: {
  spec?: ChartSpec | null;
  legacyConfig?: unknown;
  data?: unknown[];
}) {
  if (!data?.length) return null;

  for (const candidate of [spec, legacyConfig]) {
    if (specToConfig(candidate as ChartSpec)) {
      return <AnalysisChart spec={candidate as ChartSpec} data={data} />;
    }
    const config = asChartConfig(candidate);
    if (config) {
      return (
        <div style={{ marginTop: '1.5rem', padding: '1rem', background: 'rgba(0,0,0,0.1)', borderRadius: '8px' }}>
          <ChartRenderer data={data as Record<string, unknown>[]} config={config} />
        </div>
      );
    }
  }

  return null;
}
