import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, test } from 'vitest';

import { StepTrail, formatElapsed, summarise } from '@/components/analyst/StepTrail';
import { PIPELINE_STEPS, type StepEvent } from '@/types/analysis';

const ev = (over: Partial<StepEvent> & { step: StepEvent['step'] }): StepEvent => ({
  state: 'done', label: '', ...over,
} as StepEvent);

describe('formatElapsed', () => {
  test('formats seconds and milliseconds', () => {
    expect(formatElapsed(1240)).toBe('1.2s');
    expect(formatElapsed(340)).toBe('340ms');
  });

  test('is empty for zero or missing', () => {
    expect(formatElapsed(0)).toBe('');
    expect(formatElapsed(undefined)).toBe('');
  });
});

describe('StepTrail', () => {
  test('renders nothing when idle with no steps', () => {
    const { container } = render(<StepTrail steps={[]} running={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  test('renders all six steps in pipeline order before any event arrives', () => {
    render(<StepTrail steps={[]} running />);
    const items = screen.getByRole('list', { name: 'Analysis steps' })
      .querySelectorAll('li');
    expect(items).toHaveLength(PIPELINE_STEPS.length);
    expect(items[0]).toHaveTextContent(/Understanding/i);
    expect(items[3]).toHaveTextContent(/Retrieving/i);
    expect(items[5]).toHaveTextContent(/Interpreting/i);
  });

  test('a skipped step shows its reason and is not styled as an error', () => {
    render(<StepTrail running steps={[ev({
      step: 'retrieving', state: 'skipped', label: 'Retrieving',
      detail: 'the workspace already covers this',
    })]} />);
    expect(screen.getByText(/already covers this/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  test('the coverage reason is shown inline, not hidden behind a hover', () => {
    render(<StepTrail running steps={[ev({
      step: 'checking_workspace', label: 'Checking the workspace',
      detail: 'reused df_1 (412,003 rows)',
    })]} />);
    // Visible text, not a title attribute.
    expect(screen.getByText(/reused df_1 \(412,003 rows\)/)).toBeVisible();
  });

  test('elapsed time is shown on completed steps', () => {
    render(<StepTrail running steps={[ev({
      step: 'planning', label: 'Planning', elapsed_ms: 1240,
    })]} />);
    expect(screen.getByText('1.2s')).toBeInTheDocument();
  });

  test('the later event for a step wins', () => {
    render(<StepTrail running steps={[
      ev({ step: 'retrieving', state: 'start', label: 'Retrieving' }),
      ev({ step: 'retrieving', state: 'done', label: 'Retrieving', detail: '412,003 rows' }),
    ]} />);
    expect(screen.getByText(/412,003 rows/)).toBeInTheDocument();
  });

  test('a completed trail collapses to a one-line summary', async () => {
    const steps = PIPELINE_STEPS.map((step) =>
      ev({ step, label: step, detail: `did ${step}`, elapsed_ms: 400 }));

    render(<StepTrail steps={steps} running={false} />);

    expect(screen.queryByText(/did planning/)).not.toBeInTheDocument();
    const toggle = screen.getByRole('button');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    await userEvent.setup().click(toggle);
    expect(screen.getByText(/did planning/)).toBeInTheDocument();
  });
});

describe('summarise', () => {
  test('counts steps and totals their time', () => {
    const steps = [
      ev({ step: 'planning', elapsed_ms: 1000 }),
      ev({ step: 'analysing', elapsed_ms: 1400 }),
    ];
    expect(summarise(steps)).toContain('2 steps');
    expect(summarise(steps)).toContain('2.4s');
  });

  test('says when no warehouse query was needed', () => {
    // Plan A's entire value proposition, rendered as one line.
    expect(summarise([ev({ step: 'retrieving', state: 'skipped' })]))
      .toContain('no warehouse query');
  });

  test('does not claim that when the warehouse was queried', () => {
    expect(summarise([ev({ step: 'retrieving', state: 'done', detail: '412,003 rows' })]))
      .not.toContain('no warehouse query');
  });
});
