import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, test, vi } from 'vitest';

import { StepTrail, formatElapsed, runningStep, summarise } from '@/components/analyst/StepTrail';
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

describe('an abandoned step', () => {
  // The pipeline can decline a turn half-way and hand it to the legacy
  // approved-knowledge path. The trail has to show that the step closed and
  // where the turn went, or a finished turn reads as a stuck one.
  const abandoned = ev({
    step: 'analysing', state: 'abandoned',
    detail: 'no analysis was produced -- answering from approved knowledge instead',
  });

  test('is not left rendering as still running', () => {
    render(<StepTrail steps={[abandoned]} running />);
    const row = screen.getByText(/Analysing/).closest('li')!;
    expect(row.textContent).not.toContain('◌');
  });

  test('says where the turn went instead', () => {
    render(<StepTrail steps={[abandoned]} running />);
    expect(screen.getByText(/approved knowledge instead/)).toBeInTheDocument();
  });

  test('is distinct from a skipped step', () => {
    // `skipped` is good news -- it is why a turn was cheap. A fallback must not
    // be able to borrow that mark and read as an optimisation.
    render(<StepTrail steps={[abandoned]} running />);
    const mark = screen.getByText(/Analysing/).closest('li')!.firstChild!.textContent;
    expect(mark).not.toBe('–');
    expect(mark).toBe('↳');
  });

  test('is not counted as a completed step in the summary', () => {
    expect(summarise([abandoned])).toContain('0 steps');
  });

  test('does not claim the warehouse was spared', () => {
    expect(summarise([abandoned])).not.toContain('no warehouse query');
  });
});

describe('formatElapsed past a minute', () => {
  // A planning call really has been measured at 663s. "663.5s" makes the reader
  // do the arithmetic; the whole point of the trail is that they should not
  // have to.
  test('reads as minutes and seconds', () => {
    expect(formatElapsed(663_500)).toBe('11m 04s');
    expect(formatElapsed(60_000)).toBe('1m 00s');
  });

  test('leaves the short cases exactly as they were', () => {
    expect(formatElapsed(59_999)).toBe('60.0s');
    expect(formatElapsed(1240)).toBe('1.2s');
    expect(formatElapsed(340)).toBe('340ms');
  });
});

describe('runningStep', () => {
  test('is the step whose latest event is a start', () => {
    expect(runningStep([
      ev({ step: 'understanding', state: 'done' }),
      ev({ step: 'planning', state: 'start' }),
    ])).toBe('planning');
  });

  test('is null once that step closes', () => {
    expect(runningStep([
      ev({ step: 'planning', state: 'start' }),
      ev({ step: 'planning', state: 'done' }),
    ])).toBeNull();
  });

  test('is null for an empty trail', () => {
    expect(runningStep([])).toBeNull();
  });
});

describe('the running clock', () => {
  afterEach(() => { vi.useRealTimers(); });

  const running = [
    ev({ step: 'understanding', state: 'done', elapsed_ms: 212 }),
    ev({ step: 'planning', state: 'start' }),
  ];

  test('counts up on the step in flight', () => {
    vi.useFakeTimers();
    render(<StepTrail steps={running} running />);
    const row = () => screen.getByText(/Planning the turn/).closest('li')!;
    expect(row().textContent).not.toMatch(/\ds/);

    act(() => { vi.advanceTimersByTime(5000); });
    expect(row().textContent).toContain('5.0s');

    act(() => { vi.advanceTimersByTime(700_000); });
    expect(row().textContent).toContain('11m');
  });

  test('does not put a clock on a step that has finished', () => {
    vi.useFakeTimers();
    render(<StepTrail steps={running} running />);
    act(() => { vi.advanceTimersByTime(5000); });
    // Understanding closed at 212ms and must keep saying so.
    expect(screen.getByText(/Understanding the question/).closest('li')!.textContent)
      .toContain('212ms');
  });

  test('stops ticking when nothing is running', () => {
    vi.useFakeTimers();
    const done = [ev({ step: 'planning', state: 'done', elapsed_ms: 1000 })];
    render(<StepTrail steps={done} running />);
    act(() => { vi.advanceTimersByTime(10_000); });
    expect(screen.getByText(/Planning the turn/).closest('li')!.textContent)
      .toContain('1.0s');
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
