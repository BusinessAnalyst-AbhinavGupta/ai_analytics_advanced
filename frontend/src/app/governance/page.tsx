"use client";

import { useStore } from '@/store/useStore';

export default function Governance() {
  const tenantId = useStore(state => state.tenantId);
  const { retentionDays, piiDetection } = useStore(state => state.governance);
  const setGovernance = useStore(state => state.setGovernance);
  
  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '2rem', minHeight: '80vh' }}>
        <h1 style={{ marginBottom: '1rem' }}>Governance & Security</h1>
        <p style={{ color: 'var(--text-secondary)', marginBottom: '2rem' }}>
          Manage data retention policies, audit logs, and security constraints.
        </p>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: '2rem' }}>
          
          <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
            <h2 style={{ fontSize: '1.2rem', marginBottom: '1.5rem', color: 'var(--text-primary)' }}>Data Policies</h2>
            
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <h3 style={{ margin: '0 0 0.25rem 0', fontSize: '1rem', color: 'var(--text-primary)' }}>Telemetry Retention</h3>
                  <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>Days to keep observability logs</p>
                </div>
                <input 
                  type="number" 
                  value={retentionDays}
                  onChange={e => setGovernance({ retentionDays: parseInt(e.target.value) || 30 })}
                  style={{ width: '80px', padding: '0.5rem', background: 'rgba(0,0,0,0.3)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '4px', color: '#fff', textAlign: 'center' }}
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <h3 style={{ margin: '0 0 0.25rem 0', fontSize: '1rem', color: 'var(--text-primary)' }}>PII Detection & Masking</h3>
                  <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>Automatically mask sensitive data</p>
                </div>
                <button 
                  onClick={() => setPiiDetection(!piiDetection)}
                  style={{ 
                    width: '40px', height: '24px', borderRadius: '12px', border: 'none', cursor: 'pointer',
                    background: piiDetection ? 'var(--accent-primary)' : 'rgba(255,255,255,0.1)',
                    position: 'relative',
                    transition: 'background 0.3s'
                  }}
                >
                  <span style={{ 
                    position: 'absolute', top: '2px', left: piiDetection ? '18px' : '2px', 
                    width: '20px', height: '20px', background: '#fff', borderRadius: '50%',
                    transition: 'left 0.3s'
                  }}></span>
                </button>
              </div>

              <button style={{ background: 'rgba(255,255,255,0.1)', padding: '0.75rem', borderRadius: '6px', border: '1px solid rgba(255,255,255,0.1)', color: '#fff', cursor: 'pointer', marginTop: '1rem' }}>
                Save Policies
              </button>
            </div>
          </div>

          <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
            <h2 style={{ fontSize: '1.2rem', marginBottom: '1.5rem', color: 'var(--text-primary)' }}>Audit Export</h2>
            <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', marginBottom: '1.5rem' }}>
              Export a complete log of all queries run by the LLM, the tables accessed, and the users who triggered them for compliance auditing.
            </p>
            <button style={{ background: 'var(--accent-primary)', padding: '0.75rem 1.5rem', borderRadius: '6px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 600 }}>
              Export CSV
            </button>
          </div>

        </div>
      </div>
    </main>
  );
}
