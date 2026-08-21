import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, test } from 'vitest';

import { AnalysisDisclosures } from '@/components/analyst/AnalysisDisclosures';
import type { AnalysisArtifact, ExtractMeta } from '@/types/analysis';

const full: AnalysisArtifact = {
  plan_rationale: 'reuse the checkout cube and re-cut by device',
  base_view: 'checkout_sessions',
  semantics_used: ['revenue', 'country'],
  requirement: { grain: 'session_id', window: '2026-07-01..2026-07-31' },
  coverage: { decision: 'reuse', label: 'df_1', reason: 'reused df_1; device is already a dimension' },
  datasets_used: ['df_1'],
  warehouse_sql: ['SELECT country, SUM(revenue) FROM base GROUP BY country'],
  workspace_sql: ['SELECT device, SUM(revenue) FROM df_1 GROUP BY device'],
  python_code: ["result = df_1.groupby('device')['revenue'].sum()"],
  result_summary: [{ device: 'ios', revenue: 40 }],
  assumptions: [
    'service_line attributed to each session by highest intent (mobile > fixed > ott); '
    + 'rows touching multiple service_line values are counted once, under their highest-ranked one.',
  ],
};

const meta: ExtractMeta = {
  label: 'df_1', grain: ['session_id'], row_count: 412003,
  columns: ['country', 'device', 'revenue'],
};

const open = async (name: RegExp) => {
  await userEvent.setup().click(screen.getByRole('button', { name }));
};

describe('AnalysisDisclosures', () => {
  test('all four disclosures render collapsed for a full artifact', () => {
    render(<AnalysisDisclosures analysis={full} extractMeta={meta} />);
    for (const label of ['Data used', 'SQL', 'Analysis code', 'Methodology']) {
      expect(screen.getByRole('button', { name: new RegExp(label, 'i') }))
        .toHaveAttribute('aria-expanded', 'false');
    }
  });

  test('nothing sensitive is visible before a click', () => {
    render(<AnalysisDisclosures analysis={full} extractMeta={meta} />);
    expect(screen.queryByText(/SELECT/)).not.toBeInTheDocument();
    expect(screen.queryByText(/groupby/)).not.toBeInTheDocument();
  });

  test('warehouse and workspace SQL are labelled separately', async () => {
    // Not cosmetic: "this ran against Athena" and "this ran locally over cached
    // Parquet" are different claims about where a number came from.
    render(<AnalysisDisclosures analysis={full} extractMeta={meta} />);
    await open(/SQL/i);
    expect(screen.getByText('Warehouse (Athena)')).toBeInTheDocument();
    expect(screen.getByText('Workspace (DuckDB, local)')).toBeInTheDocument();
  });

  test('the coverage reason is shown verbatim', async () => {
    render(<AnalysisDisclosures analysis={full} extractMeta={meta} />);
    await open(/Data used/i);
    expect(screen.getByText(/reused df_1; device is already a dimension/)).toBeInTheDocument();
  });

  test('the grain and row count are named', async () => {
    render(<AnalysisDisclosures analysis={full} extractMeta={meta} />);
    await open(/Data used/i);
    expect(screen.getByText('one row per session_id')).toBeInTheDocument();
    expect(screen.getByText('412,003')).toBeInTheDocument();
  });

  test('a truncated extract is called out in Data used', async () => {
    render(<AnalysisDisclosures analysis={full} extractMeta={{ ...meta, truncated: true }} />);
    await open(/Data used/i);
    expect(screen.getByText(/truncated at 412,003 rows/)).toBeInTheDocument();
    expect(screen.getByText(/understated/)).toBeInTheDocument();
  });

  test('a grain violation is surfaced', async () => {
    render(<AnalysisDisclosures analysis={full} extractMeta={{ ...meta, grain_violated: true }} />);
    await open(/Data used/i);
    expect(screen.getByText(/double-counted/i)).toBeInTheDocument();
  });

  test('attribution rules are stated as sentences in Methodology', async () => {
    render(<AnalysisDisclosures analysis={full} extractMeta={meta} />);
    await open(/Methodology/i);
    expect(screen.getByText(/attributed to each session by highest intent/)).toBeInTheDocument();
  });

  test('the requirement renders as key/value rows, not raw json', async () => {
    render(<AnalysisDisclosures analysis={full} extractMeta={meta} />);
    await open(/Methodology/i);
    expect(screen.getByText('grain')).toBeInTheDocument();
    expect(screen.getByText('session_id')).toBeInTheDocument();
  });

  test('a section with no content does not render', () => {
    render(<AnalysisDisclosures analysis={{ ...full, python_code: [] }} extractMeta={meta} />);
    expect(screen.queryByRole('button', { name: /Analysis code/i })).not.toBeInTheDocument();
  });

  test('an empty section is absent rather than empty', () => {
    render(<AnalysisDisclosures
      analysis={{ ...full, warehouse_sql: [], workspace_sql: [] }} extractMeta={meta} />);
    expect(screen.queryByRole('button', { name: /^SQL/i })).not.toBeInTheDocument();
  });

  test('an undefined analysis renders nothing and does not throw', () => {
    const { container } = render(<AnalysisDisclosures />);
    expect(container).toBeEmptyDOMElement();
  });

  test('sql is rendered as text, never as html', async () => {
    render(<AnalysisDisclosures
      analysis={{ ...full, warehouse_sql: ["SELECT '<script>alert(1)</script>'"] }}
      extractMeta={meta} />);
    await open(/SQL/i);
    expect(document.querySelector('script')).toBeNull();
    expect(screen.getByText(/alert\(1\)/)).toBeInTheDocument();
  });

  test('the download slot is hosted inside Data used', async () => {
    render(<AnalysisDisclosures
      analysis={full} extractMeta={meta}
      download={<button type="button">Download df_1</button>} />);
    expect(screen.queryByRole('button', { name: /Download df_1/ })).not.toBeInTheDocument();
    await open(/Data used/i);
    expect(screen.getByRole('button', { name: /Download df_1/ })).toBeInTheDocument();
  });
});
