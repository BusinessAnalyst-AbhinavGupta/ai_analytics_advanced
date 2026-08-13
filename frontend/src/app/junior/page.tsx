"use client";

import { useEffect } from 'react';
import { useStore } from '@/store/useStore';

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
            logs.map((log, i) => (
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
                  <strong style={{ color: 'var(--text-primary)', fontSize: '1.1rem' }}>{log.stage}</strong>
                  <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)', background: 'rgba(255,255,255,0.05)', padding: '0.2rem 0.5rem', borderRadius: '4px' }}>
                    {log.duration_ms}ms
                  </span>
                </div>
                <div style={{ fontSize: '0.95rem', color: 'var(--text-secondary)', fontFamily: 'var(--font-geist-mono, monospace)' }}>
                  {log.resource}
                </div>
                {log.meta && Object.keys(log.meta).length > 0 && (
                  <div style={{ marginTop: '0.75rem', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                    {JSON.stringify(log.meta)}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      </div>
    </main>
  );
}
