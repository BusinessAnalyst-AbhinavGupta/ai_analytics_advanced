'use client';

import { useState } from 'react';

import { useStore } from '@/store/useStore';
import type { StakeholderMessage } from '@/types/analysis';

/**
 * What is genuinely global about building a report: how much is selected, what
 * it will cost, what shape it comes out in, and what went wrong.
 *
 * Selection itself moved into the thread, onto the turn it selects. A checkbox
 * next to an answer is legible; a parallel list of question stubs in a side
 * panel is a matching exercise.
 */

// Must match analytics_platform/storyline.py's WARN_TOKEN_THRESHOLD.
const WARN_TOKEN_THRESHOLD = 50_000;

/** Same formula as the backend's, so the warning agrees with it. */
export function estimateTokens(
  messages: StakeholderMessage[], selectedIds: string[],
): number {
  const text = messages
    .filter((m) => selectedIds.includes(m.answer_id))
    .map((m) => m.question + m.answer + (m.facts || []).join(' ') + (m.caveats || []).join(' '))
    .join('\n');
  return Math.floor(text.length / 4);
}

export function StorylinePanel() {
  const { messages, selectedAnswerIds, exportError } = useStore((s) => s.stakeholder);
  const selectAllAnswers = useStore((s) => s.selectAllAnswers);
  const clearSelectedAnswers = useStore((s) => s.clearSelectedAnswers);
  const exportStoryline = useStore((s) => s.exportStoryline);
  const [format, setFormat] = useState<'markdown' | 'docx'>('markdown');
  const [narrate, setNarrate] = useState(false);
  const [exporting, setExporting] = useState(false);

  const estimated = estimateTokens(messages, selectedAnswerIds);
  const overBudget = estimated > WARN_TOKEN_THRESHOLD;

  return (
    <div style={{
      width: '300px', flexShrink: 0, borderLeft: '1px solid rgba(255,255,255,0.08)',
      display: 'flex', flexDirection: 'column', height: '100%', padding: '1rem',
    }}>
      <h3 style={{ marginBottom: '0.75rem' }}>Report Builder</h3>
      <p style={{ fontSize: '0.8rem', color: 'var(--text-muted)', marginBottom: '0.75rem' }}>
        Tick the turns in the thread that tell the story.
      </p>

      <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '0.75rem' }}>
        <button
          onClick={selectAllAnswers}
          style={{
            fontSize: '0.8rem', padding: '0.3rem 0.6rem', background: 'none',
            border: '1px solid rgba(255,255,255,0.15)', borderRadius: '6px',
            color: 'var(--text-secondary)', cursor: 'pointer',
          }}
        >
          Select all
        </button>
        <button
          onClick={clearSelectedAnswers}
          style={{
            fontSize: '0.8rem', padding: '0.3rem 0.6rem', background: 'none',
            border: '1px solid rgba(255,255,255,0.15)', borderRadius: '6px',
            color: 'var(--text-secondary)', cursor: 'pointer',
          }}
        >
          Clear
        </button>
      </div>

      <div style={{ flex: 1 }} />

      <div style={{
        fontSize: '0.8rem', marginBottom: '0.5rem',
        color: overBudget ? 'var(--error)' : 'var(--text-muted)',
      }}>
        {selectedAnswerIds.length} selected · ~{estimated.toLocaleString()} estimated tokens
        {overBudget && ' — this is a large export, consider selecting fewer turns'}
      </div>

      <label style={{
        display: 'flex', gap: '0.5rem', alignItems: 'flex-start',
        fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '0.5rem',
      }}>
        <input
          type="checkbox"
          checked={narrate}
          onChange={(e) => setNarrate(e.target.checked)}
        />
        <span>
          Write a narrative
          <span style={{ display: 'block', color: 'var(--text-muted)', fontSize: '0.75rem' }}>
            Sequences the turns into one argument. Costs an extra LLM call.
          </span>
        </span>
      </label>

      <select
        aria-label="Export format"
        value={format}
        onChange={(e) => setFormat(e.target.value as 'markdown' | 'docx')}
        style={{
          marginBottom: '0.5rem', padding: '0.4rem', background: 'rgba(0,0,0,0.2)',
          border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff',
        }}
      >
        <option value="markdown">Markdown</option>
        <option value="docx">Word (.docx)</option>
      </select>

      <button
        disabled={selectedAnswerIds.length === 0 || exporting}
        onClick={async () => {
          setExporting(true);
          try {
            await exportStoryline(format, narrate);
          } finally {
            setExporting(false);
          }
        }}
        style={{
          background: 'var(--accent-primary)', padding: '0.6rem', borderRadius: '8px',
          border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 600,
        }}
      >
        {exporting ? 'Exporting…' : `Export (${selectedAnswerIds.length})`}
      </button>

      {/* Added deliberately so backend 400/404/503 failures are visible instead
          of silent -- an export button that does nothing is indistinguishable
          from a broken one. Must not regress. */}
      {exportError && (
        <div role="alert" style={{ marginTop: '0.5rem', fontSize: '0.8rem', color: 'var(--error)' }}>
          Export failed: {exportError}
        </div>
      )}
    </div>
  );
}

/** The checkbox that lives on the turn it selects. */
export function StorylineCheckbox({ answerId }: { answerId: string }) {
  const selected = useStore((s) => s.stakeholder.selectedAnswerIds.includes(answerId));
  const reportBuilderOpen = useStore((s) => s.stakeholder.reportBuilderOpen);
  const toggle = useStore((s) => s.toggleAnswerSelected);

  if (!answerId || !reportBuilderOpen) return null;

  return (
    <label style={{
      display: 'flex', gap: '0.4rem', alignItems: 'center', marginTop: '0.75rem',
      fontSize: '0.8rem', color: 'var(--text-muted)', cursor: 'pointer',
    }}>
      <input
        type="checkbox"
        checked={selected}
        onChange={() => toggle(answerId)}
      />
      Include this in the report
    </label>
  );
}
