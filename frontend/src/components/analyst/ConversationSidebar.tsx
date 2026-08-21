'use client';

import { useEffect, useState } from 'react';
import { Plus, Star, Pencil, Trash2 } from 'lucide-react';

import { useStore } from '@/store/useStore';

/**
 * Ported from StakeholderChat unchanged apart from its name. It works, and its
 * fetches already moved behind apiUrl when the store was migrated in Task 6.
 */
export function ConversationSidebar() {
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
