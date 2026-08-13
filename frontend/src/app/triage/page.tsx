"use client";

import React, { useEffect, useState } from 'react';
import { useStore } from '@/store/useStore';
import { 
  useReactTable, 
  getCoreRowModel, 
  flexRender, 
  createColumnHelper,
  getExpandedRowModel,
  RowSelectionState,
  ExpandedState
} from '@tanstack/react-table';

type QueueItem = {
  id: string;
  kind: string;
  status: string;
  title: string;
  summary: string;
  payload?: any;
  confidence?: any;
  created_at?: string;
};

const columnHelper = createColumnHelper<QueueItem>();

const columns = [
  columnHelper.display({
    id: 'select',
    header: ({ table }) => (
      <input
        type="checkbox"
        checked={table.getIsAllRowsSelected()}
        onChange={table.getToggleAllRowsSelectedHandler()}
      />
    ),
    cell: ({ row }) => (
      <input
        type="checkbox"
        checked={row.getIsSelected()}
        onChange={row.getToggleSelectedHandler()}
      />
    ),
  }),
  columnHelper.display({
    id: 'expander',
    header: () => null,
    cell: ({ row }) => (
      <button
        style={{ background: 'transparent', border: 'none', color: 'var(--accent-primary)', cursor: 'pointer' }}
        onClick={row.getToggleExpandedHandler()}
      >
        {row.getIsExpanded() ? '▼' : '▶'}
      </button>
    ),
  }),
  columnHelper.accessor('kind', {
    header: 'Kind',
    cell: info => <span style={{ textTransform: 'uppercase', fontSize: '0.8rem', color: 'var(--accent-primary)' }}>{info.getValue()}</span>,
  }),
  columnHelper.accessor('title', {
    header: 'Title',
    cell: info => <strong style={{ color: 'var(--text-primary)' }}>{info.getValue()}</strong>,
  }),
  columnHelper.accessor('summary', {
    header: 'Summary',
    cell: info => <span style={{ color: 'var(--text-secondary)' }}>{(info.getValue() || '').substring(0, 100)}...</span>,
  }),
  columnHelper.accessor('status', {
    header: 'Status',
    cell: info => <span style={{ background: 'rgba(255,255,255,0.1)', padding: '0.2rem 0.5rem', borderRadius: '4px', fontSize: '0.8rem' }}>{info.getValue()}</span>,
  })
];

export default function Triage() {
  const tenantId = useStore(state => state.tenantId);
  const { activeTab, knowledgeFilterStatus, seniorQueue, triageQueue, loading, hasFetched } = useStore(state => state.triage);
  const setTriage = useStore(state => state.setTriage);
  const approveKnowledgeNodes = useStore(state => state.approveKnowledgeNodes);
  const rejectKnowledgeNodes = useStore(state => state.rejectKnowledgeNodes);

  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [expanded, setExpanded] = useState<ExpandedState>({});

  const fetchSeniorQueue = async () => {
    try {
      const res = await fetch(`http://localhost:8000/senior/${tenantId}/queue`);
      const data = await res.json();
      setTriage({ seniorQueue: Array.isArray(data) ? data : [] });
    } catch (e) {
      console.error(e);
    }
  };

  const fetchTriageQueue = async () => {
    try {
      const statusParam = knowledgeFilterStatus === 'ALL' ? '' : `?status=${knowledgeFilterStatus}`;
      const res = await fetch(`http://localhost:8000/triage/${tenantId}/queue${statusParam}`);
      const data = await res.json();
      setTriage({ triageQueue: Array.isArray(data) ? data : [] });
    } catch (e) {
      console.error(e);
    }
  };

  useEffect(() => {
    setTriage({ loading: true });
    Promise.all([fetchSeniorQueue(), fetchTriageQueue()]).finally(() => setTriage({ loading: false, hasFetched: true }));
  }, [tenantId, knowledgeFilterStatus]);

  const table = useReactTable({
    data: triageQueue,
    columns,
    state: {
      rowSelection,
      expanded,
    },
    enableRowSelection: true,
    onRowSelectionChange: setRowSelection,
    onExpandedChange: setExpanded,
    getRowId: row => row.id,
    getRowCanExpand: () => true,
    getCoreRowModel: getCoreRowModel(),
    getExpandedRowModel: getExpandedRowModel(),
  });

  const selectedIds = Object.keys(rowSelection);

  const handleApprove = async () => {
    await approveKnowledgeNodes(selectedIds);
    setRowSelection({});
  };

  const handleReject = async () => {
    await rejectKnowledgeNodes(selectedIds);
    setRowSelection({});
  };

  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '2rem', minHeight: '80vh' }}>
        <h1 style={{ marginBottom: '2rem' }}>Triage & Brain</h1>

        <div className="flex gap-4" style={{ marginBottom: '2rem' }}>
          <button 
            style={{ background: activeTab === 'senior' ? 'var(--accent-primary)' : 'rgba(255,255,255,0.1)', padding: '0.75rem 1.5rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: 'pointer' }}
            onClick={() => setTriage({ activeTab: 'senior' })}
          >
            Senior Inbox ({seniorQueue.length})
          </button>
          <button 
            style={{ background: activeTab === 'queue' ? 'var(--accent-primary)' : 'rgba(255,255,255,0.1)', padding: '0.75rem 1.5rem', borderRadius: '8px', border: 'none', color: '#fff', cursor: 'pointer' }}
            onClick={() => setTriage({ activeTab: 'queue' })}
          >
            Knowledge Queue ({triageQueue.length})
          </button>
        </div>

        {loading ? (
          <div style={{ textAlign: 'center', padding: '2rem', color: 'var(--text-muted)' }}>Loading...</div>
        ) : (
          <div className="animate-fade-in">
            {activeTab === 'senior' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
                {seniorQueue.length === 0 ? (
                  <p style={{ color: 'var(--text-muted)' }}>No analyses awaiting review.</p>
                ) : (
                  seniorQueue.map((run, i) => (
                    <div key={i} style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.2)', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.05)' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '1rem' }}>
                        <strong style={{ color: 'var(--text-primary)' }}>Run: {run.run_id}</strong>
                        <span style={{ fontSize: '0.8rem', padding: '0.2rem 0.5rem', background: 'rgba(255,255,255,0.1)', borderRadius: '4px' }}>
                          {run.review_status || 'PENDING'}
                        </span>
                      </div>
                      <p style={{ color: 'var(--text-secondary)', marginBottom: '1rem' }}><strong>Question:</strong> {run.question}</p>
                      
                      {run.sql && (
                        <div style={{ marginBottom: '1rem', padding: '1rem', background: '#1e1e1e', borderRadius: '4px', border: '1px solid #333' }}>
                          <span style={{ fontSize: '0.8rem', color: '#888', textTransform: 'uppercase' }}>Generated SQL</span>
                          <pre style={{ margin: '0.5rem 0 0 0', overflowX: 'auto', color: '#9cdcfe', fontSize: '0.9rem' }}><code>{run.sql}</code></pre>
                        </div>
                      )}
                      
                      {run.answer && (
                        <div style={{ marginBottom: '1rem', padding: '1rem', background: 'rgba(255,255,255,0.02)', borderRadius: '4px', border: '1px solid rgba(255,255,255,0.1)' }}>
                          <span style={{ fontSize: '0.8rem', color: '#888', textTransform: 'uppercase' }}>Execution Result</span>
                          <p style={{ margin: '0.5rem 0 0 0', color: 'var(--text-primary)', fontSize: '0.9rem' }}>{run.answer}</p>
                        </div>
                      )}

                      <div style={{ display: 'flex', gap: '0.5rem' }}>
                        <button style={{ background: 'var(--success)', padding: '0.5rem 1rem', borderRadius: '4px', border: 'none', color: '#fff', cursor: 'pointer' }}>Approve</button>
                        <button style={{ background: 'var(--error)', padding: '0.5rem 1rem', borderRadius: '4px', border: 'none', color: '#fff', cursor: 'pointer' }}>Reject</button>
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}

            {activeTab === 'queue' && (
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1rem' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '1rem' }}>
                    <span style={{ color: 'var(--text-muted)' }}>Status Filter:</span>
                    <select 
                      value={knowledgeFilterStatus} 
                      onChange={(e) => setTriage({ knowledgeFilterStatus: e.target.value })}
                      style={{ background: 'rgba(0,0,0,0.3)', border: '1px solid rgba(255,255,255,0.2)', padding: '0.5rem', borderRadius: '4px', color: '#fff' }}
                    >
                      <option value="ALL">All</option>
                      <option value="CANDIDATE">Candidate</option>
                      <option value="APPROVED">Approved</option>
                      <option value="APPROVED_WITH_CAVEATS">Approved with Caveats</option>
                      <option value="REJECTED">Rejected</option>
                      <option value="REVISION_REQUIRED">Revision Required</option>
                      <option value="STALE">Stale</option>
                    </select>
                  </div>
                  {selectedIds.length > 0 && (
                    <div style={{ display: 'flex', gap: '0.5rem' }}>
                      <span style={{ color: 'var(--text-muted)', marginRight: '1rem', alignSelf: 'center' }}>
                        {selectedIds.length} selected
                      </span>
                      <button onClick={handleApprove} style={{ background: 'var(--success)', padding: '0.5rem 1rem', borderRadius: '4px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 600 }}>Approve</button>
                      <button onClick={handleReject} style={{ background: 'var(--error)', padding: '0.5rem 1rem', borderRadius: '4px', border: 'none', color: '#fff', cursor: 'pointer', fontWeight: 600 }}>Reject</button>
                    </div>
                  )}
                </div>

                {triageQueue.length === 0 ? (
                  <p style={{ color: 'var(--text-muted)' }}>Knowledge queue is empty for this status.</p>
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
                          <React.Fragment key={row.id}>
                            <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.05)', background: row.getIsSelected() ? 'rgba(255,255,255,0.05)' : 'transparent' }}>
                              {row.getVisibleCells().map(cell => (
                                <td key={cell.id} style={{ padding: '1rem' }}>
                                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                                </td>
                              ))}
                            </tr>
                            {row.getIsExpanded() && (
                              <tr>
                                <td colSpan={columns.length} style={{ padding: '1.5rem', background: 'rgba(0,0,0,0.4)', borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
                                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2rem' }}>
                                    <div>
                                      <h4 style={{ color: 'var(--accent-primary)', marginBottom: '0.5rem', fontSize: '0.9rem', textTransform: 'uppercase' }}>Payload</h4>
                                      {row.original.kind === 'DEFINITION' && row.original.payload.column ? (
                                        <div style={{ background: '#1e1e1e', padding: '1rem', borderRadius: '4px', fontSize: '0.85rem' }}>
                                          <p style={{ margin: '0 0 0.5rem 0', color: '#ce9178' }}><strong>Column:</strong> {row.original.payload.column}</p>
                                          <p style={{ margin: '0 0 0.5rem 0', color: '#ce9178' }}><strong>Values:</strong> {JSON.stringify(row.original.payload.values)}</p>
                                          {row.original.payload.via && (
                                            <p style={{ margin: '0 0 0.5rem 0', color: '#888' }}><em>Derived from Source ID: {row.original.payload.via}</em></p>
                                          )}
                                          {row.original.payload.source_sql && (
                                            <div style={{ marginTop: '1rem' }}>
                                              <strong style={{ color: '#888', textTransform: 'uppercase', fontSize: '0.75rem' }}>Provenance SQL</strong>
                                              <pre style={{ overflowX: 'auto', color: '#9cdcfe', margin: '0.5rem 0 0 0', whiteSpace: 'pre-wrap' }}>
                                                <code>{row.original.payload.source_sql}</code>
                                              </pre>
                                            </div>
                                          )}
                                        </div>
                                      ) : row.original.kind === 'QUERY' && row.original.payload.sql ? (
                                        <div style={{ background: '#1e1e1e', padding: '1rem', borderRadius: '4px', fontSize: '0.85rem' }}>
                                          <pre style={{ overflowX: 'auto', color: '#9cdcfe', margin: 0, whiteSpace: 'pre-wrap' }}>
                                            <code>{row.original.payload.sql}</code>
                                          </pre>
                                          {row.original.payload.explanation && (
                                            <p style={{ margin: '1rem 0 0 0', color: '#ce9178' }}><strong>Explanation:</strong> {row.original.payload.explanation}</p>
                                          )}
                                        </div>
                                      ) : (
                                        <pre style={{ background: '#1e1e1e', padding: '1rem', borderRadius: '4px', overflowX: 'auto', fontSize: '0.85rem', color: '#9cdcfe', margin: 0, whiteSpace: 'pre-wrap' }}>
                                          <code>{JSON.stringify(row.original.payload, null, 2)?.replace(/\\n/g, '\n')}</code>
                                        </pre>
                                      )}
                                    </div>
                                    <div>
                                      <h4 style={{ color: 'var(--accent-primary)', marginBottom: '0.5rem', fontSize: '0.9rem', textTransform: 'uppercase' }}>Details</h4>
                                      <p style={{ margin: '0 0 0.5rem 0', color: 'var(--text-secondary)' }}><strong>ID:</strong> {row.original.id}</p>
                                      <p style={{ margin: '0 0 0.5rem 0', color: 'var(--text-secondary)' }}><strong>Created:</strong> {row.original.created_at}</p>
                                      <h4 style={{ color: 'var(--accent-primary)', marginTop: '1.5rem', marginBottom: '0.5rem', fontSize: '0.9rem', textTransform: 'uppercase' }}>Confidence Scores</h4>
                                      <pre style={{ background: '#1e1e1e', padding: '1rem', borderRadius: '4px', overflowX: 'auto', fontSize: '0.85rem', color: '#ce9178', margin: 0, whiteSpace: 'pre-wrap' }}>
                                        <code>{JSON.stringify(row.original.confidence, null, 2)}</code>
                                      </pre>
                                    </div>
                                  </div>
                                </td>
                              </tr>
                            )}
                          </React.Fragment>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </main>
  );
}
