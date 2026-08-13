"use client";

import React from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { 
  BarChart2, Activity, GitCommit, Search, Shield, Target, Server, Settings, AlertCircle 
} from 'lucide-react';

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
      <h2 style={{ paddingLeft: '1rem', marginBottom: '2rem', fontSize: '1.25rem', color: 'var(--text-primary)' }}>
        Analytics AI
      </h2>
      
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
    </nav>
  );
}
