"use client";

import { useStore } from '@/store/useStore';
import { ChartRenderer } from '@/components/ChartRenderer';

export default function Home() {
  const tenantId = useStore(state => state.tenantId);
  const { question, answer, loading } = useStore(state => state.stakeholder);
  const setStakeholder = useStore(state => state.setStakeholder);

  const askStakeholder = async (overrideQuery?: string | any) => {
    // If it's an event object passed accidentally from onClick, ignore it
    const queryText = (typeof overrideQuery === 'string') ? overrideQuery : question;
    if (!queryText) return;
    setStakeholder({ loading: true });
    try {
      const res = await fetch(`http://localhost:8000/stakeholder/${tenantId}/answer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: queryText })
      });
      const data = await res.json();
      setStakeholder({ answer: data });
    } catch (e) {
      console.error(e);
    }
    setStakeholder({ loading: false });
  };
  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '2rem' }}>
        <h1 style={{ marginBottom: '2rem' }}>Stakeholder Q&A</h1>
        
        <div className="animate-fade-in">
          <p style={{ color: 'var(--text-secondary)', marginBottom: '1.5rem' }}>
            Ask a question in plain English. The AI will query the company brain, refresh approved metrics, or safely generate an answer.
          </p>
          
          <div className="flex gap-4" style={{ marginBottom: '1rem' }}>
            <input 
              type="text" 
              placeholder="E.g. What is our revenue over time?" 
              value={question}
              onChange={e => setStakeholder({ question: e.target.value })}
              onKeyDown={e => e.key === 'Enter' && askStakeholder()}
              style={{ flex: 1, padding: '0.75rem 1rem', background: 'rgba(0,0,0,0.2)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '8px', color: '#fff' }}
            />
            <button 
              onClick={() => askStakeholder()} 
              disabled={loading}
              style={{ background: 'var(--accent-primary)', padding: '0.75rem 1.5rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 600 }}
            >
              {loading ? 'Asking...' : 'Ask'}
            </button>
          </div>
          
          {answer && (
            <div style={{ marginTop: '2rem', padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '12px', border: '1px solid rgba(255,255,255,0.05)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
                <h3 style={{ margin: 0, color: 'var(--text-primary)' }}>Answer</h3>
                <span style={{ fontSize: '0.8rem', padding: '0.2rem 0.6rem', background: 'rgba(255,255,255,0.1)', borderRadius: '12px', color: 'var(--text-secondary)' }}>
                  {answer.answer_mode}
                </span>
              </div>
              
              <p style={{ color: 'var(--text-secondary)', lineHeight: 1.6 }}>{answer.answer}</p>
              
              {answer.answer_mode === 'NEEDS_CLARIFICATION' && (
                <div style={{ marginTop: '1.5rem', display: 'flex', gap: '1rem' }}>
                  <input
                    type="text"
                    id="clarification-input"
                    placeholder="Provide clarification..."
                    style={{ flex: 1, padding: '0.75rem', background: 'rgba(0,0,0,0.3)', border: '1px solid rgba(255,255,255,0.2)', borderRadius: '8px', color: '#fff' }}
                    onKeyDown={e => {
                      if (e.key === 'Enter') {
                        const val = e.currentTarget.value;
                        if(val) {
                           const newQ = question + " [Clarification: " + val + "]";
                           setStakeholder({ question: newQ });
                           askStakeholder(newQ);
                        }
                      }
                    }}
                  />
                  <button
                    onClick={() => {
                      const input = document.getElementById('clarification-input') as HTMLInputElement;
                      if(input && input.value) {
                         const newQ = question + " [Clarification: " + input.value + "]";
                         setStakeholder({ question: newQ });
                         askStakeholder(newQ);
                      }
                    }}
                    style={{ background: 'var(--accent-primary)', padding: '0.75rem 1.5rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 600 }}
                  >
                    Reply
                  </button>
                </div>
              )}
              
              {answer.chart_config && (
                <div style={{ marginTop: '2rem', padding: '1rem', background: 'rgba(0,0,0,0.1)', borderRadius: '8px' }}>
                  <ChartRenderer data={answer.chart_data || []} config={answer.chart_config} />
                </div>
              )}
              
              {answer.queries_run && answer.queries_run.length > 0 && (
                <div style={{ marginTop: '2rem' }}>
                  <h4 style={{ color: 'var(--text-muted)', fontSize: '0.9rem', marginBottom: '0.5rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>SQL Executed</h4>
                  {answer.queries_run.map((q: string, i: number) => (
                    <pre key={i} style={{ background: '#0a0a0c', padding: '1rem', borderRadius: '8px', overflowX: 'auto', fontSize: '0.85rem', color: '#a0a0a0', border: '1px solid rgba(255,255,255,0.05)' }}>
                      {q}
                    </pre>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </main>
  );
}
