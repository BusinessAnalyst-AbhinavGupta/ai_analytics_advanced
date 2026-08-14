"use client";

import { useEffect } from 'react';
import { useStore } from '@/store/useStore';

type ActivityLog = {
  stage: string;
  resource?: string;
  status?: string;
  duration_ms?: number;
  meta?: Record<string, any>;
};

// The websocket streams raw observability events -- internal stage names
// (e.g. "junior.bg_started") and JSON meta blobs meant for logs, not people.
// This turns each known stage into a plain-English headline + detail so a
// non-technical viewer (e.g. in a demo) can follow what the junior analyst
// is actually doing, without hiding the underlying data for anyone who wants it.
function describeLog(log: ActivityLog): { headline: string; detail: string } {
  const q = log.resource || '';
  switch (log.stage) {
    case 'junior.bg_started':
      return { headline: 'Started investigating a question', detail: `"${q}"` };
    case 'junior.bg_query':
      return { headline: 'Ran a query to check the data', detail: q };
    case 'junior.bg_completed':
      return {
        headline: log.status === 'FAILED' ? 'Could not finish this analysis' : 'Finished an analysis',
        detail: log.status === 'FAILED'
          ? `"${q}" -- ${log.meta?.error || 'the query could not be executed'}`
          : `"${q}" (${log.meta?.row_count ?? '?'} rows)`,
      };
    case 'junior.supporting':
      return { headline: 'Ran a supporting deep-dive', detail: `for a related "${log.meta?.category || 'analysis'}" question` };
    case 'junior.autopromote':
      return { headline: 'Added a new approved fact to the Brain', detail: `category: ${log.meta?.category || 'unknown'} (auto-approved, low-risk finding)` };
    case 'junior.autopromote_capped':
      return { headline: 'Paused auto-approving findings for today', detail: `reached the daily limit of ${log.meta?.cap ?? '?'}` };
    default:
      return { headline: log.stage, detail: q };
  }
}

export default function JuniorActivity() {
  const tenantId = useStore(state => state.tenantId);
  const { logs, isConnected } = useStore(state => state.junior);
  const setJunior = useStore(state => state.setJunior);
  const appendJuniorLog = useStore(state => state.appendJuniorLog);

  useEffect(() => {
    if (!tenantId) return;
    
    let ws: WebSocket;
    let reconnectTimer: NodeJS.Timeout;

    const connect = () => {
      ws = new WebSocket(`ws://localhost:8000/ws/tenants/${tenantId}/activity`);
      
      ws.onopen = () => {
        setJunior({ isConnected: true });
      };

      ws.onclose = () => {
        setJunior({ isConnected: false });
        // Try to reconnect in 2 seconds
        reconnectTimer = setTimeout(connect, 2000);
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          appendJuniorLog(data);
        } catch (e) {
          console.error("Failed to parse websocket message", e);
        }
      };
    };

    connect();
    
    return () => {
      clearTimeout(reconnectTimer);
      if (ws) ws.close();
    };
  }, [tenantId]);

  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '2rem', minHeight: '80vh' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '2rem' }}>
          <h1>Live Junior Activity</h1>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
            <span style={{ 
              display: 'inline-block', 
              width: '10px', 
              height: '10px', 
              borderRadius: '50%', 
              background: isConnected ? 'var(--accent-primary)' : 'var(--text-muted)',
              boxShadow: isConnected ? '0 0 10px var(--accent-primary)' : 'none',
              animation: isConnected ? 'pulse 2s infinite' : 'none'
            }}></span>
            <span style={{ color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
              {isConnected ? 'Connected via WebSocket' : 'Disconnected (Reconnecting...)'}
            </span>
          </div>
        </div>
        
        <p style={{ color: 'var(--text-secondary)', marginBottom: '2rem' }}>
          Streaming real-time telemetry from the backend autonomous worker.
        </p>
        
        <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
          {logs.length === 0 ? (
            <div style={{ padding: '4rem', textAlign: 'center', color: 'var(--text-muted)', border: '1px dashed rgba(255,255,255,0.1)', borderRadius: '12px' }}>
              Listening for activity... (Waiting for scheduler tick)
            </div>
          ) : (
            logs.map((log, i) => {
              const { headline, detail } = describeLog(log);
              return (
                <div key={i} className="animate-fade-in" style={{
                  padding: '1.25rem',
                  background: 'rgba(0,0,0,0.3)',
                  borderRadius: '8px',
                  borderLeft: log.status === 'FAILED' ? '4px solid var(--error)' : '4px solid var(--accent-primary)',
                  borderTop: '1px solid rgba(255,255,255,0.05)',
                  borderRight: '1px solid rgba(255,255,255,0.05)',
                  borderBottom: '1px solid rgba(255,255,255,0.05)'
                }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.5rem' }}>
                    <strong style={{ color: 'var(--text-primary)', fontSize: '1.1rem' }}>{headline}</strong>
                    {typeof log.duration_ms === 'number' && (
                      <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)', background: 'rgba(255,255,255,0.05)', padding: '0.2rem 0.5rem', borderRadius: '4px' }}>
                        {Math.round(log.duration_ms)}ms
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: '0.95rem', color: 'var(--text-secondary)' }}>
                    {detail}
                  </div>
                  <details style={{ marginTop: '0.75rem' }}>
                    <summary style={{ fontSize: '0.75rem', color: 'var(--text-muted)', cursor: 'pointer' }}>Technical details</summary>
                    <div style={{ marginTop: '0.5rem', fontSize: '0.8rem', color: 'var(--text-muted)', fontFamily: 'var(--font-geist-mono, monospace)' }}>
                      stage: {log.stage}
                      {log.meta && Object.keys(log.meta).length > 0 && (
                        <div style={{ marginTop: '0.25rem' }}>{JSON.stringify(log.meta)}</div>
                      )}
                    </div>
                  </details>
                </div>
              );
            })
          )}
        </div>
      </div>
    </main>
  );
}
