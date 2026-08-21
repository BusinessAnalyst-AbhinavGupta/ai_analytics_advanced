// One place that knows where the API lives. Every hardcoded
// `http://localhost:8000` in the store moves behind this, so the base URL is
// configurable at build time instead of being scattered through the codebase.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000';

export function apiUrl(path: string): string {
  const base = API_BASE.replace(/\/+$/, '');
  return `${base}/${String(path).replace(/^\/+/, '')}`;
}

/**
 * The CSV behind an answer. Every segment is encoded: a df_label is
 * backend-generated and safe today, but a conversation id or label containing a
 * slash would otherwise silently retarget the request.
 *
 * Note the frontend never builds a tenant directory or an extract filename --
 * tenant isolation is filesystem-level and is the backend's business. This only
 * names an endpoint and lets the backend validate.
 */
export function extractDownloadUrl(
  tenantId: string, conversationId: string, label: string,
): string {
  return apiUrl(
    `/stakeholder/${encodeURIComponent(tenantId)}/conversations/` +
    `${encodeURIComponent(conversationId)}/extracts/` +
    `${encodeURIComponent(label)}/download`);
}
