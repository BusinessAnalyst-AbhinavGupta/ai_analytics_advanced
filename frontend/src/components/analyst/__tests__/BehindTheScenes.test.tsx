/**
 * The panel that answers "why did it choose that".
 *
 * Two properties are load-bearing here. The first is that every entry opens
 * closed: a turn is nine or more records and several run to thousands of
 * characters, so a panel that expands them all buries the one line the reader
 * came for. The second is that the summary line makes opening unnecessary --
 * for a search that means the query string itself, which is the whole reason
 * this panel exists.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { BehindTheScenes } from '@/components/analyst/BehindTheScenes';

const TRACE = {
  answer_id: 'ans_1',
  trace_id: 'trace-1',
  records: [
    {
      seq: 1, ts: '2026-08-22T10:00:00Z', stage: 'recalling', kind: 'llm',
      duration_ms: 420, tokens_in: 40, tokens_out: 8, ok: true,
      payload: {
        system_prompt: 'You distil questions.',
        prompt: 'Extract the core analytical topic',
        response_text: 'consent drop-off',
        model: 'm', temperature: 0,
      },
    },
    {
      seq: 2, ts: '2026-08-22T10:00:01Z', stage: 'recalling', kind: 'retrieval',
      duration_ms: 5, tokens_in: 0, tokens_out: 0, ok: true,
      payload: {
        query: 'consent drop-off',
        lexical_ids: ['kn_1'], dense_ids: [], returned_ids: ['kn_1'],
        embedding_available: false,
      },
    },
  ],
};

function mockFetch(body: unknown, ok = true) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok, status: ok ? 200 : 500, json: async () => body,
  }));
}

async function openPanel() {
  render(<BehindTheScenes tenantId="t1" answerId="ans_1" />);
  fireEvent.click(screen.getByRole('button', { name: /behind the scenes/i }));
  await waitFor(() => expect(screen.getByRole('list', { name: 'Behind the scenes' })).toBeTruthy());
}

describe('BehindTheScenes', () => {
  beforeEach(() => vi.unstubAllGlobals());

  it('does not fetch until the panel is opened', () => {
    const spy = vi.fn();
    vi.stubGlobal('fetch', spy);
    render(<BehindTheScenes tenantId="t1" answerId="ans_1" />);
    expect(spy).not.toHaveBeenCalled();
  });

  it('shows the search string in the summary without opening the entry', async () => {
    mockFetch(TRACE);
    await openPanel();
    expect(screen.getByText(/searched for .*consent drop-off/i)).toBeTruthy();
  });

  it('starts every entry collapsed', async () => {
    mockFetch(TRACE);
    await openPanel();
    const entries = screen.getAllByRole('button', { expanded: false });
    expect(entries.length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText('Extract the core analytical topic')).toBeNull();
  });

  it('reveals the prompt and the response when an entry is opened', async () => {
    mockFetch(TRACE);
    await openPanel();
    fireEvent.click(screen.getAllByRole('button', { expanded: false })[0]);
    await waitFor(() =>
      expect(screen.getByText('Extract the core analytical topic')).toBeTruthy());
    expect(screen.getByText('consent drop-off', { selector: 'pre' })).toBeTruthy();
  });

  it('says so when meaning-based matching was unavailable', async () => {
    mockFetch(TRACE);
    await openPanel();
    const buttons = screen.getAllByRole('button', { expanded: false });
    fireEvent.click(buttons[buttons.length - 1]);
    await waitFor(() => expect(screen.getByText(/matched on wording only/i)).toBeTruthy());
  });

  it('renders an empty trace as an explicit statement, not silence', async () => {
    mockFetch({ ...TRACE, records: [] });
    await render(<BehindTheScenes tenantId="t1" answerId="ans_1" />);
    fireEvent.click(screen.getByRole('button', { name: /behind the scenes/i }));
    await waitFor(() => expect(screen.getByText(/no record was kept/i)).toBeTruthy());
  });

  it('a failed load does not read as a turn that did nothing', async () => {
    mockFetch({}, false);
    render(<BehindTheScenes tenantId="t1" answerId="ans_1" />);
    fireEvent.click(screen.getByRole('button', { name: /behind the scenes/i }));
    await waitFor(() => expect(screen.getByText(/could not load the record/i)).toBeTruthy());
  });

  it('renders nothing without an answer id', () => {
    const { container } = render(<BehindTheScenes tenantId="t1" answerId="" />);
    expect(container.firstChild).toBeNull();
  });
});
