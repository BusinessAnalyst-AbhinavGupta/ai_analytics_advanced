'use client';

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
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
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

function Row({ step, event }: { step: PipelineStep; event?: StepEvent }) {
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
    </li>
  );
}

export function StepTrail({ steps, running }: { steps: StepEvent[]; running: boolean }) {
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
        <Row key={step} step={step} event={latest.get(step)} />
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
