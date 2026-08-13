"use client";

import { useEffect } from 'react';
import { useStore } from '@/store/useStore';
import { useReactTable, getCoreRowModel, flexRender, createColumnHelper } from '@tanstack/react-table';

type LogItem = {
  timestamp: string;
  level: string;
  module: string;
  message: string;
};

const columnHelper = createColumnHelper<LogItem>();

const columns = [
  columnHelper.accessor('timestamp', {
    header: 'Timestamp',
    cell: info => <span style={{ color: 'var(--text-muted)', fontSize: '0.85rem' }}>{new Date(info.getValue()).toLocaleString()}</span>,
  }),
  columnHelper.accessor('level', {
    header: 'Level',
    cell: info => {
      const val = info.getValue();
      const color = val === 'ERROR' ? 'var(--error)' : val === 'WARN' ? '#eab308' : 'var(--text-secondary)';
      return <strong style={{ color }}>{val}</strong>;
    }
  }),
  columnHelper.accessor('module', {
    header: 'Module',
    cell: info => <span style={{ color: 'var(--accent-primary)', fontSize: '0.85rem' }}>{info.getValue()}</span>,
  }),
  columnHelper.accessor('message', {
    header: 'Message',
  })
];

export default function Observability() {
  const { logs, status, loading, purging, triggering, hasFetched } = useStore(state => state.observability);
  const setObservability = useStore(state => state.setObservability);

  const fetchData = async () => {
    try {
      const [logsRes, statusRes] = await Promise.all([
        fetch('http://localhost:8000/observability/logs?limit=100'),
        fetch('http://localhost:8000/observability/status')
      ]);
      const logsData = await logsRes.json();
      setObservability({ 
        logs: Array.isArray(logsData) ? logsData : [],
        status: await statusRes.json(),
        loading: false
      });
    } catch (e) {
      console.error(e);
      setObservability({ loading: false });
    }
  };

  useEffect(() => {
    if (hasFetched) return;
    setObservability({ hasFetched: true });
    fetchData();
  }, [hasFetched]);

  const handlePurge = async () => {
    setObservability({ purging: true });
    try {
      await fetch('http://localhost:8000/observability/purge', { method: 'POST' });
      await fetchData();
    } catch (e) {
      console.error(e);
    }
    setObservability({ purging: false });
  };

  const handleTriggerRun = async () => {
    setObservability({ triggering: true });
    try {
      await fetch('http://localhost:8000/observability/junior/run', { method: 'POST' });
      alert('Background Junior run triggered!');
    } catch (e) {
      console.error(e);
    }
    setObservability({ triggering: false });
  };

  const table = useReactTable({
    data: logs,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '2rem', minHeight: '80vh' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '2rem' }}>
          <div>
            <h1 style={{ marginBottom: '0.5rem' }}>Observability</h1>
            <p style={{ color: 'var(--text-secondary)' }}>System telemetry, active jobs, and background workers.</p>
          </div>
          <div style={{ display: 'flex', gap: '1rem' }}>
            <button 
              onClick={handleTriggerRun} 
              disabled={triggering}
              style={{ background: 'var(--accent-primary)', padding: '0.75rem 1.25rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 600 }}
            >
              {triggering ? 'Triggering...' : 'Force Junior Run'}
            </button>
            <button 
              onClick={handlePurge} 
              disabled={purging}
              style={{ background: 'rgba(255,100,100,0.1)', border: '1px solid var(--error)', padding: '0.75rem 1.25rem', borderRadius: '8px', color: 'var(--error)', cursor: 'pointer' }}
            >
              {purging ? 'Purging...' : 'Purge Old Logs'}
            </button>
          </div>
        </div>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '1rem', marginBottom: '2rem' }}>
          <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
            <div style={{ fontSize: '0.9rem', color: 'var(--text-muted)' }}>Background Scheduler</div>
            <div style={{ fontSize: '1.5rem', fontWeight: 600, color: status.scheduler_active ? 'var(--success)' : 'var(--error)', marginTop: '0.5rem' }}>
              {status.scheduler_active ? 'Active' : 'Inactive'}
            </div>
          </div>
          <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
            <div style={{ fontSize: '0.9rem', color: 'var(--text-muted)' }}>Last Junior Run</div>
            <div style={{ fontSize: '1.5rem', fontWeight: 600, color: 'var(--text-primary)', marginTop: '0.5rem' }}>
              {status.last_run ? new Date(status.last_run).toLocaleTimeString() : 'Never'}
            </div>
          </div>
          <div style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
            <div style={{ fontSize: '0.9rem', color: 'var(--text-muted)' }}>Log Count</div>
            <div style={{ fontSize: '1.5rem', fontWeight: 600, color: 'var(--text-primary)', marginTop: '0.5rem' }}>
              {status.log_count || 0}
            </div>
          </div>
        </div>

        {loading ? (
          <div style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>Loading logs...</div>
        ) : (
          <div style={{ overflowX: 'auto', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
              <thead style={{ background: 'rgba(255,255,255,0.05)' }}>
                {table.getHeaderGroups().map(headerGroup => (
                  <tr key={headerGroup.id}>
                    {headerGroup.headers.map(header => (
                      <th key={header.id} style={{ padding: '1rem', color: 'var(--text-muted)', fontWeight: 600, fontSize: '0.9rem', borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
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
        )}
      </div>
    </main>
  );
}
