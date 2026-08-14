"use client";

import React, { useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  BarChart2, Activity, GitCommit, Search, Shield, Target, Server, Settings, AlertCircle
} from 'lucide-react';
import { useStore } from '@/store/useStore';

type Tenant = { id: string; name: string };

function TenantSelector() {
  const tenantId = useStore(state => state.tenantId);
  const setTenantId = useStore(state => state.setTenantId);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetch('http://localhost:8000/tenants')
      .then(res => res.json())
      .then((data: Tenant[]) => {
        if (!Array.isArray(data)) throw new Error('unexpected response');
        setTenants(data);
        // Auto-select when nothing is chosen yet, or the current selection
        // doesn't exist (e.g. a stale id left over from a previous session).
        if (data.length > 0 && !data.some(t => t.id === tenantId)) {
          setTenantId(data[0].id);
        }
      })
      .catch(() => setError(true));
    // Only run once on mount -- re-fetching on every tenantId change would
    // fight the auto-select logic above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) {
    return (
      <div style={{ padding: '0.75rem 1rem', marginBottom: '1.5rem', fontSize: '0.85rem', color: 'var(--error)' }}>
        Can&apos;t reach the backend at localhost:8000
      </div>
    );
  }

  return (
    <div style={{ padding: '0 1rem', marginBottom: '1.5rem' }}>
      <label style={{ display: 'block', fontSize: '0.75rem', color: 'var(--text-muted)', textTransform: 'uppercase', marginBottom: '0.4rem' }}>
        Tenant
      </label>
      <select
        value={tenantId}
        onChange={(e) => setTenantId(e.target.value)}
        style={{ width: '100%', background: 'rgba(0,0,0,0.3)', border: '1px solid rgba(255,255,255,0.2)', padding: '0.5rem', borderRadius: '4px', color: '#fff', fontSize: '0.85rem' }}
      >
        {tenants.length === 0 && <option value="">Loading…</option>}
        {tenants.map(t => (
          <option key={t.id} value={t.id}>{t.name}</option>
        ))}
      </select>
    </div>
  );
}

type MetabaseStatus = { mode: 'live' | 'offline'; session_state?: string; detail?: string };

function MetabaseStatusIndicator() {
  const [status, setStatus] = useState<MetabaseStatus | null>(null);
  const [checking, setChecking] = useState(false);

  const check = () => {
    setChecking(true);
    fetch('http://localhost:8000/observability/metabase/status')
      .then(res => res.json())
      .then((data: MetabaseStatus) => setStatus(data))
      .catch(() => setStatus({ mode: 'offline', session_state: 'unreachable', detail: "Can't reach the backend" }))
      .finally(() => setChecking(false));
  };

  useEffect(() => {
    check();
    // Metabase's browser-cookie session can go stale at any time (no token to
    // proactively refresh) -- poll so a demo never discovers this only when a
    // query fails mid-presentation.
    const id = setInterval(check, 20000);
    return () => clearInterval(id);
  }, []);

  if (!status || status.mode === 'offline') {
    return (
      <div style={{ padding: '0.75rem 1rem', fontSize: '0.75rem', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: 'var(--text-muted)', flexShrink: 0 }} />
        <span>{status?.detail || 'Metabase: offline demo mode'}</span>
      </div>
    );
  }

  const ok = status.session_state === 'valid';
  return (
    <div
      onClick={check}
      title={status.detail || ''}
      style={{ padding: '0.75rem 1rem', fontSize: '0.75rem', color: ok ? 'var(--text-secondary)' : 'var(--error)', display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}
    >
      <span style={{
        width: '8px', height: '8px', borderRadius: '50%', flexShrink: 0,
        background: ok ? 'var(--success)' : 'var(--error)',
        boxShadow: ok ? '0 0 6px var(--success)' : 'none',
      }} />
      <span>
        {checking ? 'Checking…' : ok ? 'Metabase: connected' : 'Metabase: needs re-login (click to recheck)'}
      </span>
    </div>
  );
}

export function Sidebar() {
  const pathname = usePathname();

  const links = [
    { href: '/', label: 'Stakeholder Q&A', icon: <BarChart2 size={18} /> },
    { href: '/junior', label: 'Junior Activity', icon: <Activity size={18} /> },
    { href: '/pipeline', label: 'Pipeline / Catalog', icon: <GitCommit size={18} /> },
    { href: '/triage', label: 'Triage / Brain', icon: <AlertCircle size={18} /> },
    { href: '/research', label: 'Deep Research', icon: <Search size={18} /> },
    { href: '/kpis', label: 'Proactive KPIs', icon: <Target size={18} /> },
    { href: '/governance', label: 'Governance', icon: <Shield size={18} /> },
    { href: '/observability', label: 'Observability', icon: <Server size={18} /> },
    { href: '/config', label: 'Tenant Config', icon: <Settings size={18} /> },
  ];

  return (
    <nav className="glass-panel" style={{ width: '250px', height: '100vh', position: 'fixed', top: 0, left: 0, padding: '2rem 1rem', display: 'flex', flexDirection: 'column', gap: '0.5rem', borderRadius: 0, borderTop: 'none', borderBottom: 'none', borderLeft: 'none', zIndex: 100 }}>
      <h2 style={{ paddingLeft: '1rem', marginBottom: '1rem', fontSize: '1.25rem', color: 'var(--text-primary)' }}>
        Analytics AI
      </h2>

      <TenantSelector />

      {links.map(link => {
        const isActive = pathname === link.href;
        return (
          <Link 
            key={link.href} 
            href={link.href}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '0.75rem',
              padding: '0.75rem 1rem',
              borderRadius: '8px',
              textDecoration: 'none',
              color: isActive ? '#fff' : 'var(--text-secondary)',
              background: isActive ? 'var(--accent-primary)' : 'transparent',
              transition: 'background 0.2s, color 0.2s',
              fontWeight: isActive ? 600 : 400
            }}
          >
            {link.icon}
            {link.label}
          </Link>
        )
      })}

      <div style={{ marginTop: 'auto', borderTop: '1px solid rgba(255,255,255,0.08)', paddingTop: '0.5rem' }}>
        <MetabaseStatusIndicator />
      </div>
    </nav>
  );
}
