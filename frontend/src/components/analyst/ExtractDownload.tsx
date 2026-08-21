'use client';

import { extractDownloadUrl } from '@/lib/api';
import type { ExtractMeta } from '@/types/analysis';

/**
 * The rows behind the answer, as CSV.
 *
 * A plain <a download>: the endpoint already sets Content-Disposition and CORS
 * already exposes it. The frontend never builds a tenant path or an extract
 * filename -- tenant isolation is filesystem-level and stays the backend's job.
 */
export function ExtractDownload({
  tenantId, conversationId, meta,
}: {
  tenantId: string;
  conversationId: string;
  meta?: ExtractMeta;
}) {
  if (!meta?.label) return null;

  const rows = meta.row_count !== undefined
    ? `${meta.row_count.toLocaleString()} rows, CSV`
    : 'CSV';

  return (
    <div style={{ marginTop: '0.75rem' }}>
      <a
        href={extractDownloadUrl(tenantId, conversationId, meta.label)}
        download
        style={{
          display: 'inline-block', fontSize: '0.85rem', padding: '0.35rem 0.7rem',
          border: '1px solid rgba(255,255,255,0.15)', borderRadius: '6px',
          color: 'var(--text-secondary)', textDecoration: 'none',
        }}
      >
        Download {meta.label} ({rows})
      </a>
      {meta.truncated && (
        <p style={{ color: 'var(--error)', fontSize: '0.8rem', margin: '0.35rem 0 0' }}>
          this extract was truncated — the CSV is not the full population
        </p>
      )}
    </div>
  );
}
