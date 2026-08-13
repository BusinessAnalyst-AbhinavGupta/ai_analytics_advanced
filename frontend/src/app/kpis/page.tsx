"use client";

import { useEffect } from 'react';
import { useStore } from '@/store/useStore';
import { ChevronDown, ChevronUp } from 'lucide-react';

type KPI = {
  id?: string;
  name: string;
  description: string;
  sql_query?: string;
  threshold?: string;
  frequency?: string;
};

type Trending = {
  yesterday: string;
  last7Days: string;
  loading: boolean;
  error?: string;
};

export default function KPIs() {
  const tenantId = useStore(state => state.tenantId);
  const { kpisList: kpis, creatableKpis, loading, expandedRow, trendingData, editingSql, showAddKpi, newKpi, hasFetched } = useStore(state => state.kpis);
  const setKpis = useStore(state => state.setKpis);

  const fetchData = async () => {
    try {
      const [kpiRes, creatableRes] = await Promise.all([
        fetch(`http://localhost:8000/tenants/${tenantId}/kpis`),
        fetch(`http://localhost:8000/tenants/${tenantId}/kpis/creatable`)
      ]);
      const kpiData = await kpiRes.json();
      const creatableData = await creatableRes.json();
      
      const activeKpis = Array.isArray(kpiData) ? kpiData : [];
      const initialSqlState: Record<string, string> = {};
      activeKpis.forEach(k => { if (k.id && k.sql_query) initialSqlState[k.id] = k.sql_query; });

      setKpis({ 
        kpisList: activeKpis, 
        creatableKpis: Array.isArray(creatableData) ? creatableData : [],
        editingSql: initialSqlState,
        hasFetched: true
      });
      
      // Fetch live data for active KPIs
      activeKpis.forEach(kpi => {
        if (kpi.id) {
          fetchLiveTrending(kpi.id);
        }
      });
      
    } catch (e) {
      console.error(e);
    } finally {
      setKpis({ loading: false });
    }
  };

  const fetchLiveTrending = async (kpiId: string) => {
    setKpis({ trendingData: { ...useStore.getState().kpis.trendingData, [kpiId]: { yesterday: '-', last7Days: '-', loading: true } } });
    try {
      const res = await fetch(`http://localhost:8000/tenants/${tenantId}/kpis/${kpiId}/execute`);
      const data = await res.json();
      
      if (data.error) {
        setKpis({ trendingData: { ...useStore.getState().kpis.trendingData, [kpiId]: { yesterday: 'Err', last7Days: 'Err', loading: false, error: data.error } } });
      } else {
        setKpis({ trendingData: { ...useStore.getState().kpis.trendingData, [kpiId]: { yesterday: data.yesterday, last7Days: data.last7Days, loading: false } } });
      }
    } catch (e) {
      setKpis({ trendingData: { ...useStore.getState().kpis.trendingData, [kpiId]: { yesterday: 'Err', last7Days: 'Err', loading: false, error: String(e) } } });
    }
  };

  useEffect(() => {
    if (hasFetched) return;
    fetchData();
  }, [tenantId, hasFetched]);

  const toggleRow = (id: string) => {
    if (expandedRow === id) {
      setKpis({ expandedRow: null });
    } else {
      setKpis({ expandedRow: id });
    }
  };

  const handleRegister = async (kpi: KPI) => {
    try {
      const res = await fetch(`http://localhost:8000/tenants/${tenantId}/kpis`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: kpi.name,
          description: kpi.description,
          sql_query: kpi.sql_query,
          frequency: 'daily',
          threshold: kpi.threshold || ''
        })
      });
      if (res.ok) {
        alert(`${kpi.name} registered as Active KPI!`);
        setKpis({ 
          showAddKpi: false, 
          newKpi: { name: '', description: '', sql_query: '', threshold: '' } 
        });
        fetchData(); // Refresh lists
      } else {
        alert('Failed to register KPI.');
      }
    } catch (e) {
      console.error(e);
    }
  };

  const handleUpdateKpi = async (kpi: KPI, updates: Partial<KPI>) => {
    if (!kpi.id) return;
    try {
      const res = await fetch(`http://localhost:8000/tenants/${tenantId}/kpis/${kpi.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: kpi.name,
          description: kpi.description,
          sql_query: kpi.sql_query,
          frequency: kpi.frequency || 'daily',
          threshold: kpi.threshold || '',
          ...updates
        })
      });
      if (res.ok) {
        setKpis({ kpisList: kpis.map(k => k.id === kpi.id ? { ...k, ...updates } : k) });
        alert('KPI updated successfully.');
        if (updates.sql_query) {
          fetchLiveTrending(kpi.id); // Re-run with new query
        }
      }
    } catch (e) {
      console.error(e);
      alert('Failed to update KPI.');
    }
  };

  const renderList = (items: KPI[], isCreatable: boolean) => {
    return items.map((kpi, i) => {
      const uniqueId = kpi.id || `creatable-${i}`;
      const isExpanded = expandedRow === uniqueId;
      
      const trending = (kpi.id && trendingData[kpi.id]) 
        ? trendingData[kpi.id] 
        : { yesterday: '-', last7Days: '-', loading: false };

      return (
        <div key={uniqueId} style={{ 
          background: 'rgba(0,0,0,0.2)', 
          borderRadius: '8px', 
          border: '1px solid rgba(255,255,255,0.05)',
          overflow: 'hidden'
        }}>
          <div 
            onClick={() => toggleRow(uniqueId)}
            style={{ 
              padding: '1.5rem', 
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              cursor: 'pointer'
            }}
          >
            <div>
              <h3 style={{ margin: '0 0 0.5rem 0', color: 'var(--text-primary)' }}>{kpi.name}</h3>
              <p style={{ margin: 0, color: 'var(--text-secondary)', fontSize: '0.9rem' }}>
                {isCreatable ? 'Available to register' : (kpi.frequency || 'daily')}
              </p>
            </div>
            
            <div style={{ display: 'flex', alignItems: 'center', gap: '2rem' }}>
              
              {!isCreatable && (
                <div style={{ display: 'flex', gap: '2rem', textAlign: 'right' }}>
                  <div>
                    <span style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-muted)' }}>Yesterday</span>
                    <strong style={{ color: 'var(--text-primary)' }} title={trending.error}>
                      {trending.loading ? '...' : trending.yesterday}
                    </strong>
                  </div>
                  <div>
                    <span style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-muted)' }}>Last 7 Days</span>
                    <strong style={{ color: 'var(--text-primary)' }} title={trending.error}>
                      {trending.loading ? '...' : trending.last7Days}
                    </strong>
                  </div>
                  <div onClick={e => e.stopPropagation()}>
                    <span style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-muted)' }}>Alert Threshold</span>
                    <input 
                      type="text"
                      defaultValue={kpi.threshold || ''}
                      placeholder="e.g. < 5000"
                      onBlur={(e) => handleUpdateKpi(kpi, { threshold: e.target.value })}
                      style={{ 
                        background: 'rgba(0,0,0,0.4)', 
                        border: '1px solid rgba(255,255,255,0.1)', 
                        borderRadius: '4px', 
                        color: 'var(--accent-primary)',
                        padding: '0.2rem 0.5rem',
                        width: '100px',
                        textAlign: 'right',
                        fontWeight: 'bold'
                      }}
                    />
                  </div>
                </div>
              )}

              {isExpanded ? <ChevronUp size={20} color="var(--text-muted)" /> : <ChevronDown size={20} color="var(--text-muted)" />}
            </div>
          </div>
          
          {isExpanded && (
            <div style={{ padding: '0 1.5rem 1.5rem 1.5rem', borderTop: '1px solid rgba(255,255,255,0.05)', paddingTop: '1.5rem' }}>
              <div style={{ marginBottom: '1.5rem' }}>
                <strong style={{ color: 'var(--text-primary)', display: 'block', marginBottom: '0.5rem' }}>Natural Language Definition</strong>
                <p style={{ color: 'var(--text-secondary)', fontSize: '0.9rem', lineHeight: '1.5' }}>
                  {kpi.description || 'No description provided.'}
                </p>
              </div>
              
              <div>
                <strong style={{ color: 'var(--text-primary)', display: 'block', marginBottom: '0.5rem' }}>SQL Calculation</strong>
                {!isCreatable && kpi.id ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                    <textarea 
                      value={editingSql[kpi.id] !== undefined ? editingSql[kpi.id] : (kpi.sql_query || '')}
                      onChange={(e) => setKpis({ editingSql: { ...editingSql, [kpi.id as string]: e.target.value } })}
                      rows={4}
                      style={{ 
                        background: 'rgba(0,0,0,0.4)', 
                        padding: '1rem', 
                        borderRadius: '6px', 
                        border: '1px solid rgba(255,255,255,0.1)',
                        color: '#a5d6ff', 
                        fontSize: '0.85rem',
                        fontFamily: 'monospace',
                        width: '100%'
                      }}
                    />
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                        <button onClick={(e) => { e.stopPropagation(); kpi.id && fetchLiveTrending(kpi.id); }} style={{ background: 'transparent', border: '1px solid rgba(255,255,255,0.2)', color: 'var(--text-secondary)', padding: '0.3rem 0.6rem', borderRadius: '4px', cursor: 'pointer', marginRight: '1rem' }}>
                          Re-run live query in Metabase
                        </button>
                        {trending.error && <span style={{ color: '#ff6b6b' }}>Error: {trending.error}</span>}
                      </div>
                      <button 
                        onClick={() => handleUpdateKpi(kpi, { sql_query: editingSql[kpi.id as string] })}
                        style={{ background: 'var(--accent-primary)', color: '#fff', border: 'none', padding: '0.3rem 1rem', borderRadius: '4px', cursor: 'pointer' }}
                      >
                        Save SQL
                      </button>
                    </div>
                  </div>
                ) : (
                  <pre style={{ 
                    background: 'rgba(0,0,0,0.4)', 
                    padding: '1rem', 
                    borderRadius: '6px', 
                    color: '#a5d6ff', 
                    fontSize: '0.85rem',
                    overflowX: 'auto' 
                  }}>
                    {kpi.sql_query || 'No SQL definition available.'}
                  </pre>
                )}
              </div>

              {isCreatable && (
                <button 
                  onClick={() => handleRegister(kpi)}
                  style={{ marginTop: '1.5rem', background: 'var(--accent-primary)', padding: '0.5rem 1rem', borderRadius: '6px', border: 'none', color: '#fff', cursor: 'pointer' }}
                >
                  Register as Active KPI
                </button>
              )}
            </div>
          )}
        </div>
      );
    });
  };

  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '2rem', minHeight: '80vh' }}>
        <h1 style={{ marginBottom: '1rem' }}>Proactive KPI Monitoring</h1>
        <p style={{ color: 'var(--text-secondary)', marginBottom: '2rem' }}>
          Manage the core metrics that the junior analyst monitors proactively for anomalies. 
          Click on any metric to view and edit its SQL definition.
        </p>
        
        {loading ? (
          <p style={{ color: 'var(--text-muted)' }}>Loading KPIs...</p>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '3rem' }}>
            
            <section>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
                <h2 style={{ fontSize: '1.1rem', margin: 0, color: 'var(--text-primary)' }}>Registered KPI Tracking</h2>
                <button 
                  onClick={() => setKpis({ showAddKpi: !showAddKpi })}
                  style={{ background: 'rgba(255,255,255,0.1)', padding: '0.5rem 1rem', borderRadius: '4px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 500 }}
                >
                  {showAddKpi ? 'Cancel' : '+ New Custom KPI'}
                </button>
              </div>

              {showAddKpi && (
                <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.4)', borderRadius: '8px', border: '1px solid var(--accent-primary)', marginBottom: '1.5rem' }}>
                  <h3 style={{ margin: '0 0 1rem 0' }}>Define Custom KPI</h3>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                    <input 
                      type="text" placeholder="KPI Name" value={newKpi.name} onChange={e => setKpis({ newKpi: {...newKpi, name: e.target.value} })} 
                      style={{ padding: '0.75rem', background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }} 
                    />
                    <input 
                      type="text" placeholder="Alert Threshold (e.g. < 500)" value={newKpi.threshold} onChange={e => setKpis({ newKpi: {...newKpi, threshold: e.target.value} })} 
                      style={{ padding: '0.75rem', background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }} 
                    />
                    <textarea 
                      placeholder="Description (Natural Language)" value={newKpi.description} onChange={e => setKpis({ newKpi: {...newKpi, description: e.target.value} })} 
                      rows={2} style={{ gridColumn: '1 / -1', padding: '0.75rem', background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }} 
                    />
                    <textarea 
                      placeholder="SQL Query (SELECT ...)" value={newKpi.sql_query} onChange={e => setKpis({ newKpi: {...newKpi, sql_query: e.target.value} })} 
                      rows={3} style={{ gridColumn: '1 / -1', padding: '0.75rem', background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff', fontFamily: 'monospace' }} 
                    />
                    <div style={{ gridColumn: '1 / -1', display: 'flex', justifyContent: 'flex-end', marginTop: '0.5rem' }}>
                      <button 
                        onClick={() => handleRegister(newKpi)}
                        style={{ background: 'var(--accent-primary)', padding: '0.5rem 1rem', borderRadius: '6px', border: 'none', color: '#fff', cursor: 'pointer' }}
                      >
                        Save and Track Custom KPI
                      </button>
                    </div>
                  </div>
                </div>
              )}

              <div style={{ display: 'grid', gap: '1rem' }}>
                {kpis.length > 0 ? renderList(kpis, false) : (
                  <p style={{ color: 'var(--text-muted)' }}>No active KPIs being tracked.</p>
                )}
              </div>
            </section>

            <section>
              <h2 style={{ fontSize: '1.1rem', marginBottom: '1rem', color: 'var(--text-primary)' }}>Available Metrics from Knowledge Graph</h2>
              <div style={{ display: 'grid', gap: '1rem' }}>
                {creatableKpis.length > 0 ? renderList(creatableKpis, true) : (
                  <p style={{ color: 'var(--text-muted)' }}>No candidate metrics found in the Brain.</p>
                )}
              </div>
            </section>

          </div>
        )}
      </div>
    </main>
  );
}
