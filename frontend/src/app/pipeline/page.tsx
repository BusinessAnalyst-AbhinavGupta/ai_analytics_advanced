"use client";

import { useEffect } from 'react';
import { useStore } from '@/store/useStore';

export default function Pipeline() {
  const tenantId = useStore(state => state.tenantId);
  const { catalog, loading, refreshing, hasFetched } = useStore(state => state.pipeline);
  const setPipeline = useStore(state => state.setPipeline);

  const fetchCatalog = async () => {
    try {
      const res = await fetch(`http://localhost:8000/junior/${tenantId}/catalog`);
      const data = await res.json();
      setPipeline({ catalog: data, loading: false });
      return data;
    } catch (err) {
      console.error("Failed to load catalog", err);
      setPipeline({ loading: false });
      return null;
    }
  };

  const refreshCatalog = async () => {
    setPipeline({ refreshing: true });
    try {
      const res = await fetch(`http://localhost:8000/junior/${tenantId}/catalog/refresh`, {
        method: 'POST'
      });
      const data = await res.json();
      setPipeline({ catalog: data });
      sessionStorage.setItem(`catalog_refreshed_${tenantId}`, 'true');
    } catch (err) {
      console.error("Failed to refresh catalog", err);
    } finally {
      setPipeline({ refreshing: false });
    }
  };

  useEffect(() => {
    if (hasFetched) return;
    setPipeline({ hasFetched: true });
    
    fetchCatalog().then((data) => {
      // If we got an empty catalog (0 tables described) or we haven't refreshed this session, trigger a refresh
      const needsRefresh = !sessionStorage.getItem(`catalog_refreshed_${tenantId}`);
      if (needsRefresh) {
        refreshCatalog();
      }
    });
  }, [tenantId]);

  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '2rem', minHeight: '80vh' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
          <h1 style={{ margin: 0 }}>Pipeline / Catalog</h1>
          <button 
            onClick={refreshCatalog} 
            disabled={refreshing || loading}
            style={{
              padding: '0.5rem 1rem',
              background: refreshing ? 'rgba(255,255,255,0.1)' : 'var(--accent)',
              color: refreshing ? 'var(--text-muted)' : '#fff',
              border: 'none',
              borderRadius: '4px',
              cursor: refreshing || loading ? 'not-allowed' : 'pointer',
              fontWeight: 500,
              display: 'flex',
              alignItems: 'center',
              gap: '0.5rem',
              transition: 'all 0.2s'
            }}
          >
            {refreshing ? 'Refreshing...' : 'Refresh Catalog'}
          </button>
        </div>
        <p style={{ color: 'var(--text-secondary)', marginBottom: '2rem' }}>
          Explore the current schema and EDA catalog built by the Junior Analyst.
          {refreshing && <span style={{ color: 'var(--accent)', marginLeft: '1rem' }}>Updating with new tables...</span>}
        </p>

        {loading ? (
          <div style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>
            Loading catalog...
          </div>
        ) : catalog ? (
          <div className="animate-fade-in" style={{ display: 'grid', gap: '1.5rem' }}>
            <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
              <h2 style={{ fontSize: '1.1rem', marginBottom: '1rem', color: 'var(--text-primary)' }}>Schema Overview</h2>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '1rem' }}>
                {catalog.tables && Array.isArray(catalog.tables) && catalog.tables.length > 0 ? catalog.tables.map((tableData: any) => (
                  <div key={tableData.table} style={{ background: 'rgba(255,255,255,0.02)', padding: '1rem', borderRadius: '6px' }}>
                    <h3 style={{ margin: '0 0 0.5rem 0', fontSize: '1rem' }}>{tableData.table}</h3>
                    <p style={{ margin: 0, fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                      Columns: {tableData.columns.length}
                    </p>
                    {tableData.error && (
                      <p style={{ margin: '0.5rem 0 0 0', fontSize: '0.75rem', color: 'var(--error)' }}>
                        {tableData.error}
                      </p>
                    )}
                  </div>
                )) : (
                  <span style={{ color: 'var(--text-muted)' }}>No tables discovered yet. Click Refresh to probe the database.</span>
                )}
              </div>
            </div>

            <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
              <h2 style={{ fontSize: '1.1rem', marginBottom: '1rem', color: 'var(--text-primary)' }}>Raw JSON Catalog</h2>
              <pre style={{ 
                background: '#0a0a0c', 
                padding: '1rem', 
                borderRadius: '8px', 
                overflowX: 'auto', 
                fontSize: '0.85rem', 
                color: '#a0a0a0', 
                border: '1px solid rgba(255,255,255,0.05)',
                maxHeight: '400px',
                overflowY: 'auto'
              }}>
                {JSON.stringify(catalog, null, 2)}
              </pre>
            </div>
          </div>
        ) : (
          <div style={{ color: 'var(--error)' }}>Failed to load catalog.</div>
        )}
      </div>
    </main>
  );
}
