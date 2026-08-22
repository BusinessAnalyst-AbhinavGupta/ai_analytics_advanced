'use client';

import { useCallback, useEffect, useState } from 'react';

import { Disclosure } from '@/components/analyst/Disclosure';
import { answerTraceUrl } from '@/lib/api';
import { STEP_LABELS } from '@/types/analysis';
import type { AnswerTrace, TraceRecord } from '@/types/analysis';

/**
 * What the analyst actually did, in the order it did it.
 *
 * The other disclosures answer "what was computed". This one answers "why did
 * it choose that" -- the question the artifact could never reach, because
 * nothing recorded a prompt or a search string until now.
 *
 * Every entry starts collapsed. A turn is nine or more records and several of
 * them are thousands of characters; opening them all by default would bury the
 * one line a reader came for. The summary line is chosen so the whole turn can
 * be scanned without opening anything -- for a search that means the query
 * string itself, which is the field this panel exists to expose.
 *
 * Fetched lazily, on first open. A trace is large and most answers are never
 * questioned, so paying for it on render would tax every answer to serve a few.
 */

function str(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  return typeof value === 'string' ? value : '';
}

function ids(payload: Record<string, unknown>, key: string): string[] {
  const value = payload[key];
  return Array.isArray(value) ? value.map(String) : [];
}

function stageLabel(stage: string): string {
  return STEP_LABELS[stage] ?? stage;
}

/** The one line that has to make opening it unnecessary. */
function summarise(record: TraceRecord): string {
  const { payload } = record;
  if (record.kind === 'retrieval') {
    const query = str(payload, 'query');
    const found = ids(payload, 'returned_ids').length;
    return `searched for “${query}” — found ${found}`;
  }
  const response = str(payload, 'response_text').replace(/\s+/g, ' ').trim();
  if (response) return response.length > 110 ? `${response.slice(0, 110)}…` : response;
  return str(payload, 'error') || 'no response';
}

function Block({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div style={{ marginTop: '0.5rem' }}>
      <div style={{ fontSize: '0.75rem', color: 'var(--text-muted)', marginBottom: '0.2rem' }}>
        {label}
      </div>
      <pre
        style={{
          margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          fontSize: '0.8rem', lineHeight: 1.5, color: 'var(--text-secondary)',
          maxHeight: '22rem', overflowY: 'auto',
        }}
      >
        {value}
      </pre>
    </div>
  );
}

function Truncated({ payload, field }: { payload: Record<string, unknown>; field: string }) {
  if (payload[`${field}_truncated`] !== true) return null;
  const len = payload[`${field}_len`];
  return (
    <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)', margin: '0.2rem 0 0' }}>
      shortened for storage — the original was {String(len)} characters
    </p>
  );
}

function Entry({ record }: { record: TraceRecord }) {
  const [open, setOpen] = useState(false);
  const { payload } = record;
  const seconds = (record.duration_ms / 1000).toFixed(1);

  return (
    <li style={{ borderTop: '0.5px solid var(--border)', padding: '0.5rem 0' }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        style={{
          display: 'block', width: '100%', textAlign: 'left', background: 'none',
          border: 'none', padding: 0, cursor: 'pointer',
        }}
      >
        <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
          {open ? '▾' : '▸'} {stageLabel(record.stage)} · {seconds}s
          {record.ok ? '' : ' · failed'}
        </span>
        <span
          style={{
            display: 'block', fontSize: '0.85rem', color: 'var(--text-secondary)',
            marginTop: '0.15rem',
          }}
        >
          {summarise(record)}
        </span>
      </button>

      {open && record.kind === 'retrieval' && (
        <div style={{ marginTop: '0.4rem' }}>
          <Block label="Searched for" value={str(payload, 'query')} />
          <Block
            label="Matched on wording"
            value={ids(payload, 'lexical_ids').join(', ') || 'nothing'}
          />
          <Block
            label="Matched on meaning"
            value={
              payload.embedding_available === true
                ? (ids(payload, 'dense_ids').join(', ') || 'nothing')
                : 'not available — this search matched on wording only'
            }
          />
          <Block
            label="Handed to the analyst"
            value={ids(payload, 'returned_ids').join(', ') || 'nothing'}
          />
        </div>
      )}

      {open && record.kind !== 'retrieval' && (
        <div style={{ marginTop: '0.4rem' }}>
          <Block label="Instructions given" value={str(payload, 'system_prompt')} />
          <Truncated payload={payload} field="system_prompt" />
          <Block label="What we asked" value={str(payload, 'prompt')} />
          <Truncated payload={payload} field="prompt" />
          <Block label="What it answered" value={str(payload, 'response_text')} />
          <Truncated payload={payload} field="response_text" />
          <Block label="Error" value={str(payload, 'error')} />
        </div>
      )}
    </li>
  );
}

export function BehindTheScenes({
  tenantId, answerId,
}: {
  tenantId: string;
  answerId: string;
}) {
  const [trace, setTrace] = useState<AnswerTrace | null>(null);
  const [error, setError] = useState('');
  const [asked, setAsked] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch(answerTraceUrl(tenantId, answerId));
      if (!res.ok) throw new Error(String(res.status));
      setTrace((await res.json()) as AnswerTrace);
    } catch {
      // A trace that will not load must not look like a turn that did nothing.
      setError('Could not load the record for this answer.');
    }
  }, [tenantId, answerId]);

  useEffect(() => {
    if (asked && !trace && !error) void load();
  }, [asked, trace, error, load]);

  if (!tenantId || !answerId) return null;

  const records = trace?.records ?? [];

  return (
    <div onFocusCapture={() => setAsked(true)} onClickCapture={() => setAsked(true)}>
      <Disclosure label="Behind the scenes" count={records.length || undefined}>
        {error && (
          <p style={{ fontSize: '0.85rem', color: 'var(--error)', margin: '0.25rem 0' }}>
            {error}
          </p>
        )}
        {!error && !trace && (
          <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', margin: '0.25rem 0' }}>
            Loading…
          </p>
        )}
        {trace && records.length === 0 && (
          <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', margin: '0.25rem 0' }}>
            No record was kept for this answer.
          </p>
        )}
        {records.length > 0 && (
          <ul aria-label="Behind the scenes" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
            {records.map((record) => (
              <Entry key={record.seq} record={record} />
            ))}
          </ul>
        )}
      </Disclosure>
    </div>
  );
}
