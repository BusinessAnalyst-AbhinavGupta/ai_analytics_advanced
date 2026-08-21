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

/**
 * Chart for one turn, preferring the artifact and falling back to the legacy
 * path so historical turns keep their charts.
 *
 * The discrimination is on *shape*, not on which field is populated, because
 * `chart_config` is not one thing. stakeholder.py assigns it the neutral
 * artifact spec on the analyst path (`out["chart_config"] = artifact.chart_spec`)
 * and a recharts-shaped config on the older synthesis paths. Testing for
 * `type` + `xKey` tells the two apart without guessing.
 */
export function MessageChart({
  spec, legacyConfig, data,
}: {
  spec?: ChartSpec | null;
  legacyConfig?: unknown;
  data?: unknown[];
}) {
  if (specToConfig(spec)) {
    return <AnalysisChart spec={spec} data={data} />;
  }

  const legacy = legacyConfig as Partial<ChartConfig> | undefined;
  if (legacy?.type && legacy?.xKey && Array.isArray(legacy?.series) && data?.length) {
    return (
      <div style={{ marginTop: '1.5rem', padding: '1rem', background: 'rgba(0,0,0,0.1)', borderRadius: '8px' }}>
        <ChartRenderer data={data as Record<string, unknown>[]} config={legacy as ChartConfig} />
      </div>
    );
  }

  // The neutral spec may also arrive only via chart_config on an older row.
  const asSpec = legacyConfig as ChartSpec | undefined;
  if (asSpec?.kind && specToConfig(asSpec)) {
    return <AnalysisChart spec={asSpec} data={data} />;
  }

  return null;
}
