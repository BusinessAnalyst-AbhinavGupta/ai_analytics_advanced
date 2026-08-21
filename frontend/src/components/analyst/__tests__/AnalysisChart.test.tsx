import { render, screen } from '@testing-library/react';
import { describe, expect, test } from 'vitest';

import { AnalysisChart, specToConfig } from '@/components/analyst/AnalysisChart';
import { ExtractDownload } from '@/components/analyst/ExtractDownload';

describe('specToConfig', () => {
  test('maps kind to a recharts type', () => {
    expect(specToConfig({ kind: 'bar', x: 'country', y: 'revenue' })!.type).toBe('BarChart');
    expect(specToConfig({ kind: 'line', x: 'd', y: 'r' })!.type).toBe('LineChart');
    expect(specToConfig({ kind: 'area', x: 'd', y: 'r' })!.type).toBe('AreaChart');
    expect(specToConfig({ kind: 'scatter', x: 'd', y: 'r' })!.type).toBe('ScatterChart');
  });

  test('kind matching is case-insensitive', () => {
    expect(specToConfig({ kind: 'LINE', x: 'd', y: 'r' })!.type).toBe('LineChart');
  });

  test('x becomes xKey', () => {
    expect(specToConfig({ kind: 'bar', x: 'country', y: 'revenue' })!.xKey).toBe('country');
  });

  test('an array y becomes one series per key', () => {
    expect(specToConfig({ kind: 'line', x: 'd', y: ['a', 'b'] })!.series.map((s) => s.key))
      .toEqual(['a', 'b']);
  });

  test('an unknown kind renders nothing rather than guessing', () => {
    // A wrong chart is worse than no chart.
    expect(specToConfig({ kind: 'sankey', x: 'a', y: 'b' })).toBeNull();
  });

  test('a missing x or empty y yields null', () => {
    expect(specToConfig({ kind: 'bar', x: '', y: 'r' })).toBeNull();
    expect(specToConfig({ kind: 'bar', x: 'd', y: [] })).toBeNull();
  });

  test('a missing spec yields null', () => {
    expect(specToConfig(null)).toBeNull();
    expect(specToConfig(undefined)).toBeNull();
  });
});

describe('AnalysisChart', () => {
  test('renders nothing without data', () => {
    const { container } = render(
      <AnalysisChart spec={{ kind: 'bar', x: 'd', y: 'r' }} data={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  test('renders nothing for an unknown kind even with data', () => {
    const { container } = render(
      <AnalysisChart spec={{ kind: 'sankey', x: 'd', y: 'r' }} data={[{ d: 1, r: 2 }]} />);
    expect(container).toBeEmptyDOMElement();
  });

  test('a grouping series is surfaced in the caption, not dropped', () => {
    render(<AnalysisChart
      spec={{ kind: 'bar', x: 'd', y: 'r', series: 'service_line' }}
      data={[{ d: 1, r: 2 }]} />);
    expect(screen.getByText(/service_line/)).toBeInTheDocument();
  });

  test('the spec title is shown', () => {
    render(<AnalysisChart
      spec={{ kind: 'bar', x: 'd', y: 'r', title: 'Revenue by day' }}
      data={[{ d: 1, r: 2 }]} />);
    expect(screen.getByText(/Revenue by day/)).toBeInTheDocument();
  });
});

describe('ExtractDownload', () => {
  test('the link points at the extract endpoint and names the row count', () => {
    render(<ExtractDownload tenantId="t" conversationId="c1"
                            meta={{ label: 'df_1', row_count: 412003 }} />);
    const a = screen.getByRole('link');
    expect(a).toHaveAttribute('href', expect.stringContaining('/extracts/df_1/download'));
    expect(a).toHaveTextContent('412,003');
  });

  test('a truncated extract says so next to the download', () => {
    render(<ExtractDownload tenantId="t" conversationId="c1"
                            meta={{ label: 'df_1', row_count: 10, truncated: true }} />);
    expect(screen.getByText(/not the full population/)).toBeInTheDocument();
  });

  test('renders nothing without a label', () => {
    const { container } = render(
      <ExtractDownload tenantId="t" conversationId="c1" meta={{ label: '' }} />);
    expect(container).toBeEmptyDOMElement();
  });

  test('path segments are encoded', () => {
    render(<ExtractDownload tenantId="t/x" conversationId="c 1"
                            meta={{ label: 'df_1', row_count: 1 }} />);
    expect(screen.getByRole('link')).toHaveAttribute('href', expect.stringContaining('t%2Fx'));
  });
});
