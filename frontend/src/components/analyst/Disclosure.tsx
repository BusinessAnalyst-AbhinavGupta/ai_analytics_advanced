'use client';

import { useId, useState, type ReactNode } from 'react';

/**
 * The one collapsible primitive every provenance section shares, replacing the
 * old CollapsibleCode.
 *
 * Always collapsed by default. That is the hard constraint from the plan, not a
 * preference: a stakeholder reading an answer should see a paragraph and a
 * chart, and a stakeholder challenged on that answer should be able to open
 * every layer beneath it in seconds. A UI that dumps SQL into the thread by
 * default fails the first reader; one that hides it entirely fails the second.
 */
export function Disclosure({
  label, count, children, tone = 'default',
}: {
  label: string;
  count?: number;
  children: ReactNode;
  tone?: 'default' | 'warning';
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();

  return (
    <div style={{ marginTop: '0.75rem' }}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={panelId}
        style={{
          display: 'flex', alignItems: 'center', gap: '0.4rem', background: 'none',
          border: 'none', cursor: 'pointer', padding: 0, fontSize: '0.8rem',
          textTransform: 'uppercase', letterSpacing: '0.05em',
          color: tone === 'warning' ? 'var(--error)' : 'var(--text-muted)',
        }}
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        {label}
        {count !== undefined && count > 1 && (
          <span
            style={{
              fontSize: '0.7rem', padding: '0 0.35rem', borderRadius: '8px',
              background: 'rgba(255,255,255,0.1)',
            }}
          >
            {count}
          </span>
        )}
      </button>
      {open && (
        <div id={panelId} style={{ marginTop: '0.5rem' }}>
          {children}
        </div>
      )}
    </div>
  );
}

/**
 * Code inside a disclosure. Always text, never dangerouslySetInnerHTML: this is
 * model output and warehouse-derived SQL, and one day it will contain a
 * <script>. It scrolls inside its own container so a wide query cannot make the
 * whole page scroll sideways.
 */
export function CodeBlock({ code, label }: { code: string; label?: string }) {
  return (
    <div style={{ marginBottom: '0.75rem' }}>
      {label && (
        <div style={{
          fontSize: '0.7rem', color: 'var(--text-muted)', marginBottom: '0.25rem',
          textTransform: 'uppercase', letterSpacing: '0.05em',
        }}>
          {label}
        </div>
      )}
      <pre style={{
        background: '#0a0a0c', padding: '1rem', borderRadius: '8px',
        overflowX: 'auto', maxWidth: '100%', fontSize: '0.85rem', color: '#a0a0a0',
        border: '1px solid rgba(255,255,255,0.05)', margin: 0,
      }}>
        <code>{code}</code>
      </pre>
    </div>
  );
}
