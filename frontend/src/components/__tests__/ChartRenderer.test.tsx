import { render, screen } from '@testing-library/react';
import { describe, expect, test } from 'vitest';

import { ChartRenderer } from '@/components/ChartRenderer';

// A real test of existing code, to prove the harness itself works: the jsdom
// environment, the @/ alias, JSX transform, and jest-dom matchers all at once.
//
// Note what is asserted. recharts' ResponsiveContainer measures its parent, and
// in jsdom that parent has zero size, so the chart body renders nothing at all.
// Anything beyond the empty state has to be tested through the pure adapter
// instead of the rendered SVG -- which is why Task 11 exports specToConfig
// separately.
describe('ChartRenderer', () => {
  test('renders a placeholder when there is no data', () => {
    render(<ChartRenderer data={[]} config={{ type: 'BarChart', xKey: 'x', series: [] }} />);
    expect(screen.getByText('No data available')).toBeInTheDocument();
  });
});
