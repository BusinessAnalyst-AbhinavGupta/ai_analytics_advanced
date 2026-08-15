"use client";

import { useEffect, useRef, useState } from 'react';
import { useStore } from '@/store/useStore';
import { ChartRenderer } from '@/components/ChartRenderer';
import { Plus, Star, Pencil, Trash2, ThumbsUp, ThumbsDown } from 'lucide-react';

function CollapsibleCode({ label, code }: { label: string; code: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ marginTop: '1rem' }}>
      <button
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', fontSize: '0.8rem', textTransform: 'uppercase', letterSpacing: '0.05em', padding: 0 }}
      >
        <span style={{ display: 'inline-block', transition: 'transform 0.15s', transform: open ? 'rotate(90deg)' : 'none' }}>▶</span>
        {label}
      </button>
      {open && (
        <pre style={{ background: '#0a0a0c', padding: '1rem', borderRadius: '8px', overflowX: 'auto', fontSize: '0.85rem', color: '#a0a0a0', border: '1px solid rgba(255,255,255,0.05)', marginTop: '0.5rem' }}>
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
}

function ConversationHistorySidebar() {
  const { conversations, conversationsLoading, activeConversationId } = useStore(state => state.stakeholder);
  const fetchConversations = useStore(state => state.fetchConversations);
  const loadConversation = useStore(state => state.loadConversation);
  const startNewConversation = useStore(state => state.startNewConversation);
  const renameConversation = useStore(state => state.renameConversation);
  const starConversation = useStore(state => state.starConversation);
  const deleteConversation = useStore(state => state.deleteConversation);
  const tenantId = useStore(state => state.tenantId);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState('');

  useEffect(() => { fetchConversations(); startNewConversation(); }, [tenantId, fetchConversations, startNewConversation]);

  const commitRename = (id: string) => {
    const title = editingTitle.trim();
    setEditingId(null);
    if (title) renameConversation(id, title);
  };

  return (
    <div style={{ width: '260px', flexShrink: 0, borderRight: '1px solid rgba(255,255,255,0.08)', display: 'flex', flexDirection: 'column', height: '100%' }}>
      <button
        onClick={() => startNewConversation()}
        style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', margin: '1rem', padding: '0.6rem 0.9rem', background: 'var(--accent-primary)', border: 'none', borderRadius: '8px', color: '#fff', fontWeight: 600, cursor: 'pointer' }}
      >
        <Plus size={16} /> New chat
      </button>
      <div style={{ overflowY: 'auto', flex: 1, padding: '0 0.5rem' }}>
        {conversationsLoading && conversations.length === 0 && (
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', padding: '0.5rem 0.75rem' }}>Loading…</p>
        )}
        {conversations.map(c => {
          const active = c.id === activeConversationId;
          return (
            <div
              key={c.id}
              onClick={() => editingId !== c.id && loadConversation(c.id)}
              style={{
                display: 'flex', alignItems: 'center', gap: '0.4rem', padding: '0.6rem 0.75rem',
                borderRadius: '8px', cursor: 'pointer', marginBottom: '0.2rem',
                background: active ? 'rgba(255,255,255,0.08)' : 'transparent',
              }}
            >
              {editingId === c.id ? (
                <input
                  autoFocus
                  value={editingTitle}
                  onChange={e => setEditingTitle(e.target.value)}
                  onBlur={() => commitRename(c.id)}
                  onKeyDown={e => { if (e.key === 'Enter') commitRename(c.id); if (e.key === 'Escape') setEditingId(null); }}
                  style={{ flex: 1, background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.2)', borderRadius: '4px', color: '#fff', padding: '0.2rem 0.4rem', fontSize: '0.85rem' }}
                />
              ) : (
                <span style={{ flex: 1, fontSize: '0.85rem', color: active ? 'var(--text-primary)' : 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {c.title}
                </span>
              )}
              <button
                title={c.starred ? 'Unstar' : 'Star'}
                onClick={(e) => { e.stopPropagation(); starConversation(c.id, !c.starred); }}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: c.starred ? '#f5c518' : 'var(--text-muted)', padding: 2, display: 'flex' }}
              >
                <Star size={14} fill={c.starred ? '#f5c518' : 'none'} />
              </button>
              <button
                title="Rename"
                onClick={(e) => { e.stopPropagation(); setEditingId(c.id); setEditingTitle(c.title); }}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', padding: 2, display: 'flex' }}
              >
                <Pencil size={14} />
              </button>
              <button
                title="Delete"
                onClick={(e) => { e.stopPropagation(); if (confirm(`Delete "${c.title}"?`)) deleteConversation(c.id); }}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--text-muted)', padding: 2, display: 'flex' }}
              >
                <Trash2 size={14} />
              </button>
            </div>
          );
        })}
        {!conversationsLoading && conversations.length === 0 && (
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', padding: '0.5rem 0.75rem' }}>No conversations yet.</p>
        )}
      </div>
    </div>
  );
}

export function StakeholderChat() {
  const { question, loading, messages } = useStore(state => state.stakeholder);
  const setStakeholder = useStore(state => state.setStakeholder);
  const askStakeholder = useStore(state => state.askStakeholder);
  const submitFeedback = useStore(state => state.submitFeedback);
  const threadEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages.length]);

  return (
    <div style={{ display: 'flex', height: 'calc(100vh - 4rem)' }}>
      <ConversationHistorySidebar />
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div style={{ flex: 1, overflowY: 'auto', padding: '1.5rem' }}>
          {messages.length === 0 && (
            <p style={{ color: 'var(--text-secondary)' }}>
              Ask a question in plain English. The AI will query the company brain, refresh approved metrics, or safely generate an answer.
            </p>
          )}
          {messages.map((m, i) => (
            <div key={m.answer_id || i} style={{ marginBottom: '1.5rem' }}>
              <p style={{ color: 'var(--text-primary)', fontWeight: 600, marginBottom: '0.5rem' }}>{m.question}</p>
              <div style={{ padding: '1.25rem', background: 'rgba(0,0,0,0.2)', borderRadius: '12px', border: '1px solid rgba(255,255,255,0.05)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.75rem' }}>
                  <span style={{ fontSize: '0.8rem', padding: '0.2rem 0.6rem', background: 'rgba(255,255,255,0.1)', borderRadius: '12px', color: 'var(--text-secondary)' }}>
                    {m.answer_mode}
                  </span>
                </div>
                <p style={{ color: 'var(--text-secondary)', lineHeight: 1.6 }}>{m.answer}</p>
                {m.answer_id && (
                  <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1rem' }}>
                    <button
                      title="Good answer"
                      aria-label="Good answer"
                      aria-pressed={m.feedback === 'up'}
                      onClick={() => submitFeedback(m.answer_id, 'up')}
                      style={{ background: m.feedback === 'up' ? 'rgba(34,197,94,0.15)' : 'none', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', padding: '0.3rem 0.5rem', cursor: 'pointer', color: m.feedback === 'up' ? 'var(--success)' : 'var(--text-muted)', display: 'flex' }}
                    >
                      <ThumbsUp size={14} />
                    </button>
                    <button
                      title="Bad answer"
                      aria-label="Bad answer"
                      aria-pressed={m.feedback === 'down'}
                      onClick={() => submitFeedback(m.answer_id, 'down')}
                      style={{ background: m.feedback === 'down' ? 'rgba(239,68,68,0.15)' : 'none', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', padding: '0.3rem 0.5rem', cursor: 'pointer', color: m.feedback === 'down' ? 'var(--error)' : 'var(--text-muted)', display: 'flex' }}
                    >
                      <ThumbsDown size={14} />
                    </button>
                  </div>
                )}
                {m.chart_config && (
                  <div style={{ marginTop: '1.5rem', padding: '1rem', background: 'rgba(0,0,0,0.1)', borderRadius: '8px' }}>
                    <ChartRenderer data={m.chart_data || []} config={m.chart_config} />
                  </div>
                )}
                {m.queries_run && m.queries_run.length > 0 && (
                  m.queries_run.map((q, qi) => (
                    <CollapsibleCode key={qi} label={`SQL executed${m.queries_run.length > 1 ? ` (${qi + 1}/${m.queries_run.length})` : ''}`} code={q} />
                  ))
                )}
                {m.python_cells && m.python_cells.length > 0 && (
                  m.python_cells.map((p, pi) => (
                    <CollapsibleCode
                      key={pi}
                      label={`Python executed${m.python_cells!.length > 1 ? ` (${pi + 1}/${m.python_cells!.length})` : ''}`}
                      code={p.code}
                    />
                  ))
                )}
              </div>
            </div>
          ))}
          <div ref={threadEndRef} />
        </div>
        <div style={{ display: 'flex', gap: '1rem', padding: '1.5rem', borderTop: '1px solid rgba(255,255,255,0.08)' }}>
          <input
            type="text"
            placeholder="E.g. What is our revenue over time?"
            value={question}
            onChange={e => setStakeholder({ question: e.target.value })}
            onKeyDown={e => e.key === 'Enter' && askStakeholder(question)}
            style={{ flex: 1, padding: '0.75rem 1rem', background: 'rgba(0,0,0,0.2)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '8px', color: '#fff' }}
          />
          <button
            onClick={() => askStakeholder(question)}
            disabled={loading}
            style={{ background: 'var(--accent-primary)', padding: '0.75rem 1.5rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 600 }}
          >
            {loading ? 'Asking...' : 'Ask'}
          </button>
        </div>
      </div>
    </div>
  );
}
