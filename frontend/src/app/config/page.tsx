"use client";

import { useEffect } from 'react';
import { useStore } from '@/store/useStore';
import { useReactTable, getCoreRowModel, flexRender, createColumnHelper } from '@tanstack/react-table';

type Target = {
  name: string;
  description: string;
  category: string;
  priority: number;
  target_value: number | string;
};

type AnalystToggle = { enabled: boolean; provider: string; model: string; role: string };
type AnalystConfig = {
  junior: AnalystToggle;
  senior: AnalystToggle;
  stakeholder: AnalystToggle;
  junior_depth: number;
  human_signoff_days: number;
};

const columnHelper = createColumnHelper<Target>();

const columns = [
  columnHelper.accessor('name', {
    header: 'Name',
    cell: info => <strong style={{ color: 'var(--text-primary)' }}>{info.getValue()}</strong>,
  }),
  columnHelper.accessor('category', {
    header: 'Category',
    cell: info => <span style={{ textTransform: 'uppercase', fontSize: '0.8rem', color: 'var(--accent-primary)' }}>{info.getValue()}</span>,
  }),
  columnHelper.accessor('priority', {
    header: 'Priority',
  }),
  columnHelper.accessor('target_value', {
    header: 'Target Value',
  }),
  columnHelper.accessor('description', {
    header: 'Description',
    cell: info => <span style={{ color: 'var(--text-secondary)' }}>{info.getValue()}</span>,
  })
];

export default function Config() {
  const tenantId = useStore(state => state.tenantId);
  const { profile, targets, aiConfig, loading, saving, savingProfile, showAddTarget, newTarget, hasFetched } = useStore(state => state.config);
  const setConfig = useStore(state => state.setConfig);



  const fetchData = async () => {
    try {
      const [profileRes, configRes] = await Promise.all([
        fetch(`http://localhost:8000/tenants/${tenantId}`), // Fixed endpoint
        fetch(`http://localhost:8000/tenants/${tenantId}/analyst-config`)
      ]);
      const profileData = await profileRes.json();
      const configData = await configRes.json();
      
      const p = profileData.profile || {};
      
      const updates: Partial<any> = {
        profile: p,
        targets: p.targets || [],
        loading: false,
        hasFetched: true
      };
      
      if (!configData.detail) {
        updates.aiConfig = configData;
      }
      setConfig(updates);
    } catch (err) {
      console.error(err);
      setConfig({ loading: false });
    }
  };

  useEffect(() => {
    if (hasFetched) return;
    fetchData();
  }, [tenantId, hasFetched]);

  const saveAiConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!aiConfig) return;
    setConfig({ saving: true });
    try {
      await fetch(`http://localhost:8000/tenants/${tenantId}/analyst-config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(aiConfig)
      });
      alert('AI Configuration saved successfully!');
    } catch (err) {
      console.error(err);
      alert('Failed to save config.');
    } finally {
      setConfig({ saving: false });
    }
  };

  const saveProfile = async (updatedProfile: any) => {
    setConfig({ savingProfile: true });
    try {
      const res = await fetch(`http://localhost:8000/tenants/${tenantId}/company-profile`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(updatedProfile)
      });
      if (!res.ok) throw new Error("Failed to save profile");
      alert('Business Profile saved successfully!');
      fetchData();
    } catch (err) {
      console.error(err);
      alert('Failed to save profile.');
    } finally {
      setConfig({ savingProfile: false });
    }
  };

  const handleProfileSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    saveProfile(profile);
  };

  const handleAddTarget = async (e: React.FormEvent) => {
    e.preventDefault();
    
    // Add to local list and save to profile
    const { sql_query, ...targetToSave } = newTarget;
    const updatedTargets = [...targets, targetToSave];
    const updatedProfile = { ...profile, targets: updatedTargets };
    setConfig({ targets: updatedTargets, profile: updatedProfile, showAddTarget: false });
    
    // Save profile to backend
    await saveProfile(updatedProfile);

    // Auto-register to KPI Tracking
    const finalSql = newTarget.sql_query.trim() !== '' 
        ? newTarget.sql_query 
        : `-- Auto-generated metric query for ${newTarget.name}\nSELECT 0 AS value;`;

    try {
      await fetch(`http://localhost:8000/tenants/${tenantId}/kpis`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: `OKR: ${newTarget.name}`,
          description: `Auto-registered from OKR: ${newTarget.description}`,
          sql_query: finalSql,
          frequency: 'daily',
          threshold: String(newTarget.target_value)
        })
      });
      alert('Target added to Profile and auto-registered for KPI tracking!');
      setConfig({ newTarget: { name: '', category: 'growth', priority: 1, target_value: '', description: '', sql_query: '' } });
    } catch (err) {
      console.error('Failed to auto-register KPI', err);
    }
  };

  const table = useReactTable({
    data: targets,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  const renderRoleConfig = (role: 'junior' | 'senior' | 'stakeholder') => {
    if (!aiConfig) return null;
    const config = aiConfig[role];
    
    return (
      <div key={role} style={{ display: 'grid', gridTemplateColumns: '1fr 2fr 3fr', gap: '1rem', alignItems: 'center', marginBottom: '1rem' }}>
        <div style={{ textTransform: 'capitalize', fontWeight: 600, color: 'var(--text-primary)' }}>{role} AI</div>
        
        <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
          <input 
            type="checkbox" 
            checked={config.enabled} 
            onChange={e => setConfig({ aiConfig: { ...aiConfig, [role]: { ...config, enabled: e.target.checked } } })}
          />
          <span style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>Enabled</span>
        </label>
        
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <input 
            type="text" 
            placeholder="Provider (e.g. openrouter)" 
            value={config.provider}
            onChange={e => setConfig({ aiConfig: { ...aiConfig, [role]: { ...config, provider: e.target.value } } })}
            style={{ flex: 1, padding: '0.5rem', background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '4px', color: '#fff' }}
          />
          <input 
            type="text" 
            placeholder="Model (e.g. anthropic/claude-3-haiku)" 
            value={config.model}
            onChange={e => setConfig({ aiConfig: { ...aiConfig, [role]: { ...config, model: e.target.value } } })}
            style={{ flex: 2, padding: '0.5rem', background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '4px', color: '#fff' }}
          />
        </div>
      </div>
    );
  };

  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '2rem', minHeight: '80vh' }}>
        <h1 style={{ marginBottom: '2rem' }}>Tenant Configuration</h1>
        
        {loading ? (
          <div style={{ color: 'var(--text-muted)' }}>Loading...</div>
        ) : (
          <div className="animate-fade-in" style={{ display: 'grid', gap: '2rem' }}>
            
            {/* AI Config */}
            {aiConfig && (
              <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
                <h2 style={{ fontSize: '1.2rem', marginBottom: '1.5rem', color: 'var(--text-primary)' }}>Analyst AI Config (Models & Stages)</h2>
                <form onSubmit={saveAiConfig}>
                  {renderRoleConfig('junior')}
                  {renderRoleConfig('senior')}
                  {renderRoleConfig('stakeholder')}

                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2rem', marginTop: '2rem', padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '6px' }}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                      <label style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>Junior Depth (0=Basic, 1=Standard, 2=Advanced)</label>
                      <input 
                        type="number" 
                        min={0} max={2}
                        value={aiConfig.junior_depth}
                        onChange={e => setConfig({ aiConfig: { ...aiConfig, junior_depth: parseInt(e.target.value) } })}
                        style={{ padding: '0.75rem', background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }}
                      />
                    </div>
                    
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                      <label style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>Human Signoff Window (Days)</label>
                      <input 
                        type="number" 
                        min={1} max={30}
                        value={aiConfig.human_signoff_days}
                        onChange={e => setConfig({ aiConfig: { ...aiConfig, human_signoff_days: parseInt(e.target.value) } })}
                        style={{ padding: '0.75rem', background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }}
                      />
                    </div>
                  </div>
                  
                  <button type="submit" disabled={saving} style={{ marginTop: '1.5rem', background: 'var(--accent-primary)', padding: '0.75rem 1.5rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: saving ? 'not-allowed' : 'pointer', fontWeight: 600 }}>
                    {saving ? 'Saving...' : 'Save AI Config'}
                  </button>
                </form>
              </div>
            )}

            {/* Tenant Profile Form */}
            <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
              <h2 style={{ fontSize: '1.2rem', marginBottom: '1.5rem', color: 'var(--text-primary)' }}>Business Context</h2>
              <form onSubmit={handleProfileSubmit} style={{ display: 'grid', gap: '1rem', maxWidth: '600px' }}>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                  <label style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>Company Description</label>
                  <textarea 
                    value={profile.description || ''}
                    onChange={e => setConfig({ profile: {...profile, description: e.target.value} })}
                    rows={3}
                    style={{ padding: '0.75rem', background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }}
                  />
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                  <label style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>Competitors</label>
                  <input 
                    type="text" 
                    value={(profile.competitors || []).join(", ")}
                    onChange={e => setConfig({ profile: {...profile, competitors: e.target.value.split(',').map((s: string) => s.trim()).filter(Boolean)} })}
                    style={{ padding: '0.75rem', background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }}
                  />
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                  <label style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>Risks</label>
                  <input 
                    type="text" 
                    value={(profile.risks || []).join(", ")}
                    onChange={e => setConfig({ profile: {...profile, risks: e.target.value.split(',').map((s: string) => s.trim()).filter(Boolean)} })}
                    style={{ padding: '0.75rem', background: 'rgba(0,0,0,0.4)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }}
                  />
                </div>
                
                <button type="submit" disabled={savingProfile} style={{ justifySelf: 'flex-start', background: 'var(--accent-primary)', padding: '0.75rem 1.5rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: savingProfile ? 'not-allowed' : 'pointer', fontWeight: 600, marginTop: '1rem' }}>
                  {savingProfile ? 'Saving...' : 'Save Profile'}
                </button>
              </form>
            </div>

            {/* OKRs Table */}
            <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem' }}>
                <h2 style={{ fontSize: '1.2rem', margin: 0, color: 'var(--text-primary)' }}>Business Targets & OKRs</h2>
                <button 
                  onClick={() => setConfig({ showAddTarget: true })}
                  style={{ background: 'rgba(255,255,255,0.1)', padding: '0.5rem 1rem', borderRadius: '4px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 500 }}
                >
                  + Add Target
                </button>
              </div>

              {showAddTarget && (
                <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.4)', borderRadius: '8px', border: '1px solid var(--accent-primary)', marginBottom: '1.5rem' }}>
                  <h3 style={{ margin: '0 0 1rem 0' }}>Define New OKR</h3>
                  <form onSubmit={handleAddTarget} style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                    <input required type="text" placeholder="Metric Name (e.g. ARR)" value={newTarget.name} onChange={e => setConfig({ newTarget: {...newTarget, name: e.target.value} })} style={{ padding: '0.75rem', background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }} />
                    <input required type="text" placeholder="Target Value (e.g. 5M)" value={newTarget.target_value} onChange={e => setConfig({ newTarget: {...newTarget, target_value: e.target.value} })} style={{ padding: '0.75rem', background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }} />
                    <select value={newTarget.category} onChange={e => setConfig({ newTarget: {...newTarget, category: e.target.value} })} style={{ padding: '0.75rem', background: '#1e1e1e', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }}>
                      <option value="growth">Growth</option>
                      <option value="retention">Retention</option>
                      <option value="efficiency">Efficiency</option>
                    </select>
                    <input required type="number" min={1} placeholder="Priority (1-5)" value={newTarget.priority} onChange={e => setConfig({ newTarget: {...newTarget, priority: parseInt(e.target.value)} })} style={{ padding: '0.75rem', background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }} />
                    <input required type="text" placeholder="Description" value={newTarget.description} onChange={e => setConfig({ newTarget: {...newTarget, description: e.target.value} })} style={{ gridColumn: '1 / -1', padding: '0.75rem', background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff' }} />
                    
                    <div style={{ gridColumn: '1 / -1', display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                      <label style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>SQL Query (Optional - Leave blank to auto-generate or provide exact SQL to calculate this OKR)</label>
                      <textarea 
                        value={newTarget.sql_query} 
                        onChange={e => setConfig({ newTarget: {...newTarget, sql_query: e.target.value} })} 
                        placeholder="SELECT count(*) FROM ..."
                        rows={3}
                        style={{ padding: '0.75rem', background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px', color: '#fff', fontFamily: 'monospace' }}
                      />
                    </div>

                    <div style={{ gridColumn: '1 / -1', display: 'flex', gap: '1rem', justifyContent: 'flex-end', marginTop: '0.5rem' }}>
                      <button type="button" onClick={() => setConfig({ showAddTarget: false })} style={{ background: 'transparent', color: '#fff', border: 'none', cursor: 'pointer' }}>Cancel</button>
                      <button type="submit" style={{ background: 'var(--accent-primary)', padding: '0.5rem 1rem', borderRadius: '6px', border: 'none', color: '#fff', cursor: 'pointer' }}>Save Target & Auto-Track KPI</button>
                    </div>
                  </form>
                </div>
              )}

              {targets.length > 0 ? (
                <div style={{ overflowX: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
                    <thead>
                      {table.getHeaderGroups().map(headerGroup => (
                        <tr key={headerGroup.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
                          {headerGroup.headers.map(header => (
                            <th key={header.id} style={{ padding: '1rem', color: 'var(--text-secondary)', fontWeight: 500 }}>
                              {flexRender(header.column.columnDef.header, header.getContext())}
                            </th>
                          ))}
                        </tr>
                      ))}
                    </thead>
                    <tbody>
                      {table.getRowModel().rows.map(row => (
                        <tr key={row.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                          {row.getVisibleCells().map(cell => (
                            <td key={cell.id} style={{ padding: '1rem' }}>
                              {flexRender(cell.column.columnDef.cell, cell.getContext())}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p style={{ color: 'var(--text-muted)' }}>No targets configured for this tenant.</p>
              )}
            </div>

          </div>
        )}
      </div>
    </main>
  );
}
