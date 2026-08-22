'use client';

import { useEffect, useState } from 'react';

import { Disclosure } from '@/components/analyst/Disclosure';
import { PIPELINE_STEPS, type PipelineStep, type StepEvent } from '@/types/analysis';

/**
 * The pipeline, while it runs.
 *
 * This is about honesty rather than polish. A retrieve turn costs a planning
 * call, a schema build that may profile tables inline, a warehouse round trip,
 * and an analysis call -- a long time to show a spinner labelled "Asking...".
 * Naming what is happening tells the user what the system is spending their
 * time and money on, and makes a stall diagnosable instead of mysterious.
 */

const LABELS: Record<PipelineStep, string> = {
  understanding: 'Understanding the question',
  planning: 'Planning the turn',
  checking_workspace: 'Checking the workspace',
  retrieving: 'Retrieving',
  analysing: 'Analysing',
  interpreting: 'Interpreting',
};

// `skipped` and `abandoned` are deliberately distinct. A skipped step was never
// run and that is the good news -- it is why the turn was cheap. An abandoned
// step ran, produced nothing usable, and the turn continued down another path.
// Collapsing the two would let a fallback masquerade as an optimisation.
type State = 'pending' | 'start' | 'done' | 'skipped' | 'abandoned';

const MARK: Record<State, string> = {
  pending: '·', start: '◌', done: '✓', skipped: '–', abandoned: '↳',
};

export function formatElapsed(ms?: number): string {
  if (!ms || ms <= 0) return '';
  // Past a minute, "663.5s" is a number the reader has to do arithmetic on. A
  // planning call really can run this long -- see the OpenRouter note in
  // docs -- so the long case is worth formatting properly rather than treating
  // as an anomaly.
  if (ms >= 60_000) {
    const total = Math.round(ms / 1000);
    return `${Math.floor(total / 60)}m ${String(total % 60).padStart(2, '0')}s`;
  }
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

/** The step currently in flight, or null when nothing is running. */
export function runningStep(steps: StepEvent[]): PipelineStep | null {
  for (const [step, ev] of latestByStep(steps)) {
    if (ev.state === 'start') return step;
  }
  return null;
}

/**
 * How long the step in flight has been running, ticking once a second.
 *
 * This is the difference between "the analyst is thinking" and "this thing is
 * broken". A planning call has been measured at eleven minutes; with only a
 * label on screen, that is indistinguishable from a hang, and the honest fix is
 * to show the clock rather than to start cutting calls off.
 *
 * Purely a display concern: it measures when the `start` event *arrived*, asks
 * nothing of the server, and cancels nothing.
 */
function useRunningElapsed(step: PipelineStep | null): number {
  // Tagged with the step it was measured for. Without the tag, a step that ends
  // leaves its elapsed time on screen for up to a second under the *next*
  // step's label -- a small lie, but this component's whole job is not telling
  // those. Untagged reads as 0, which is true: it has been running under a
  // second.
  const [measured, setMeasured] = useState<{ step: PipelineStep | null; ms: number }>(
    { step: null, ms: 0 });

  // The clock is read and advanced entirely inside the timer callback. Reading
  // it during render would make render impure, and setting state synchronously
  // in the effect body would cascade a second render on every step boundary.
  useEffect(() => {
    if (!step) return;
    const began = Date.now();
    const id = setInterval(() => setMeasured({ step, ms: Date.now() - began }), 1000);
    return () => clearInterval(id);
  }, [step]);

  return step && measured.step === step ? measured.ms : 0;
}

/** The last event wins: a step goes start -> done, and only the latest matters. */
export function latestByStep(steps: StepEvent[]): Map<PipelineStep, StepEvent> {
  const out = new Map<PipelineStep, StepEvent>();
  for (const s of steps) out.set(s.step, s);
  return out;
}

export function summarise(steps: StepEvent[]): string {
  const latest = [...latestByStep(steps).values()];
  const done = latest.filter((s) => s.state === 'done');
  const total = done.reduce((acc, s) => acc + (s.elapsed_ms ?? 0), 0);
  const parts = [`${done.length} step${done.length === 1 ? '' : 's'}`];
  if (total > 0) parts.push(formatElapsed(total));
  // The line worth reading: this is why the turn was cheap.
  if (latest.some((s) => s.step === 'retrieving' && s.state === 'skipped')) {
    parts.push('no warehouse query');
  }
  return parts.join(' · ');
}

function Row({ step, event, runningFor }: {
  step: PipelineStep; event?: StepEvent; runningFor?: number;
}) {
  const state: State = (event?.state as State) ?? 'pending';
  const skipped = state === 'skipped' || state === 'abandoned';
  const color = state === 'pending' ? 'var(--text-muted)'
    : skipped ? 'var(--text-muted)'
    : 'var(--text-secondary)';

  return (
    <li style={{ display: 'flex', gap: '0.5rem', alignItems: 'baseline', padding: '0.15rem 0' }}>
      <span aria-hidden="true" style={{ color, width: '1rem', flexShrink: 0 }}>
        {MARK[state]}
      </span>
      <span style={{ color, fontStyle: skipped ? 'italic' : 'normal' }}>
        {event?.label || LABELS[step]}
        {/* detail is shown inline and always -- it is the payload, not a tooltip */}
        {event?.detail ? ` — ${event.detail}` : ''}
      </span>
      {state === 'done' && formatElapsed(event?.elapsed_ms) && (
        <span style={{ color: 'var(--text-muted)', fontSize: '0.75rem', marginLeft: 'auto' }}>
          {formatElapsed(event?.elapsed_ms)}
        </span>
      )}
      {/* The running clock. aria-live so a screen reader is told the turn is
          still moving, rather than being left in silence for minutes. */}
      {state === 'start' && !!runningFor && formatElapsed(runningFor) && (
        <span
          aria-live="polite"
          style={{ color: 'var(--text-muted)', fontSize: '0.75rem', marginLeft: 'auto' }}
        >
          {formatElapsed(runningFor)}
        </span>
      )}
    </li>
  );
}

export function StepTrail({ steps, running }: { steps: StepEvent[]; running: boolean }) {
  // Above the early return: a hook may not be called conditionally, and this
  // component genuinely does render nothing when idle with an empty trail.
  const activeStep = runningStep(steps);
  const runningFor = useRunningElapsed(activeStep);

  if (!steps.length && !running) return null;

  const latest = latestByStep(steps);

  const list = (
    <ul
      aria-label="Analysis steps"
      style={{
        listStyle: 'none', margin: 0, padding: 0, fontSize: '0.85rem',
        // A trail that animates is decoration; respect a user who has asked for
        // less of it.
        transition: 'opacity 0.15s',
      }}
    >
      {PIPELINE_STEPS.map((step) => (
        <Row key={step} step={step} event={latest.get(step)}
             runningFor={step === activeStep ? runningFor : undefined} />
      ))}
    </ul>
  );

  // A finished trail sitting expanded above every historical answer is exactly
  // the clutter this UI is meant to avoid, so it collapses to one line.
  if (!running) {
    return <Disclosure label={summarise(steps)}>{list}</Disclosure>;
  }

  return (
    <div style={{
      margin: '1rem 0', padding: '0.75rem 1rem', borderRadius: '8px',
      background: 'rgba(0,0,0,0.15)', border: '1px solid rgba(255,255,255,0.05)',
    }}>
      {list}
    </div>
  );
}
