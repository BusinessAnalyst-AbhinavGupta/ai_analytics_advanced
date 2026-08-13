"use client";

import { useStore } from '@/store/useStore';

export default function Research() {
  const tenantId = useStore(state => state.tenantId);
  const { file, query, results, loading, uploadStatus } = useStore(state => state.research);
  const setResearch = useStore(state => state.setResearch);

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    
    setResearch({ uploadStatus: 'Uploading...' });
    const formData = new FormData();
    formData.append('file', file);
    
    try {
      const res = await fetch(`http://localhost:8000/tenants/${tenantId}/ingest/file`, {
        method: 'POST',
        body: formData
      });
      const data = await res.json();
      setResearch({ uploadStatus: `Uploaded successfully: ${data.id}`, file: null });
    } catch (err: any) {
      setResearch({ uploadStatus: `Upload failed: ${err.message}` });
    }
  };

  const handleSearch = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!query) return;
    
    setResearch({ loading: true });
    try {
      const res = await fetch(`http://localhost:8000/tenants/${tenantId}/ingest/search?q=${encodeURIComponent(query)}`);
      const data = await res.json();
      setResearch({ results: data });
    } catch (err) {
      console.error(err);
    }
    setResearch({ loading: false });
  };

  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '2rem', minHeight: '80vh' }}>
        <h1 style={{ marginBottom: '2rem' }}>Deep Research & Ingestion</h1>
        
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2rem' }}>
          {/* File Upload Section */}
          <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
            <h2 style={{ fontSize: '1.2rem', marginBottom: '1rem', color: 'var(--text-primary)' }}>Document Ingestion</h2>
            <p style={{ color: 'var(--text-secondary)', marginBottom: '1.5rem', fontSize: '0.9rem' }}>
              Upload unstructured PDFs or text files to process them into the Company Brain.
            </p>
            <form onSubmit={handleUpload} style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
              <input 
                type="file" 
                onChange={e => setResearch({ file: e.target.files?.[0] || null })}
                style={{ padding: '1rem', border: '1px dashed rgba(255,255,255,0.2)', borderRadius: '8px' }}
              />
              <button 
                type="submit" 
                disabled={!file}
                style={{ background: 'var(--accent-primary)', padding: '0.75rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: file ? 'pointer' : 'not-allowed', opacity: file ? 1 : 0.5 }}
              >
                Upload Document
              </button>
              {uploadStatus && <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>{uploadStatus}</div>}
            </form>
          </div>

          {/* Search Section */}
          <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
            <h2 style={{ fontSize: '1.2rem', marginBottom: '1rem', color: 'var(--text-primary)' }}>Source Retrieval</h2>
            <p style={{ color: 'var(--text-secondary)', marginBottom: '1.5rem', fontSize: '0.9rem' }}>
              Search previously ingested unstructured documents.
            </p>
            <form onSubmit={handleSearch} style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem' }}>
              <input 
                type="text" 
                value={query}
                onChange={e => setResearch({ query: e.target.value })}
                placeholder="Search queries..."
                style={{ flex: 1, padding: '0.75rem 1rem', background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '8px', color: '#fff' }}
              />
              <button 
                type="submit"
                disabled={loading}
                style={{ background: 'rgba(255,255,255,0.1)', padding: '0.75rem 1rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: 'pointer' }}
              >
                Search
              </button>
            </form>
            
            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
              {results.length > 0 ? results.map((r, i) => (
                <div key={i} style={{ padding: '1rem', background: 'rgba(255,255,255,0.02)', borderRadius: '6px' }}>
                  <strong>{r.file_name}</strong>
                  <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)', marginTop: '0.25rem' }}>{r.snippet}</p>
                </div>
              )) : query && !loading ? (
                <div style={{ color: 'var(--text-muted)' }}>No results found.</div>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
