import { beforeEach, describe, expect, test, vi } from 'vitest';

/**
 * Stopping a turn is the user's decision and nobody else's.
 *
 * The system never aborts on its own -- there is no timer anywhere in this path
 * that cancels a call, because a long turn is usually a model thinking hard
 * rather than a failure, and cutting it off throws away work that was about to
 * land. What the user gets instead is the clock in the trail and a Stop button.
 *
 * These tests pin that bargain from the store's side: a signal is handed to the
 * stream, nothing trips it but `stopStreaming`, and a turn the user walked away
 * from cannot come back and switch off the spinner of the turn after it.
 */

const captured: { signal?: AbortSignal }[] = [];
let release: (() => void) | null = null;

vi.mock('@/lib/streamAnswer', () => ({
  streamAnswer: vi.fn(async (
    _t: string, _q: string, _c: string, _h: unknown, signal?: AbortSignal,
  ) => {
    captured.push({ signal });
    // Hang like a real slow turn until the test decides otherwise.
    await new Promise<void>((resolve) => { release = resolve; });
  }),
}));

const { useStore } = await import('@/store/useStore');

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('stopStreaming', () => {
  beforeEach(() => {
    captured.length = 0;
    release = null;
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => [] })));
    useStore.getState().setTenantId('tnt_test');
    useStore.getState().startNewConversation();
  });

  test('a turn in flight is given a signal that nothing has tripped', async () => {
    void useStore.getState().askStakeholder('why are purchases failing?');
    await flush();
    expect(captured).toHaveLength(1);
    expect(captured[0].signal).toBeInstanceOf(AbortSignal);
    expect(captured[0].signal!.aborted).toBe(false);
    expect(useStore.getState().stakeholder.loading).toBe(true);
  });

  test('no timer trips it on its own', async () => {
    vi.useFakeTimers();
    void useStore.getState().askStakeholder('why are purchases failing?');
    await vi.advanceTimersByTimeAsync(0);
    // Well past any timeout anyone might be tempted to add.
    await vi.advanceTimersByTimeAsync(20 * 60 * 1000);
    expect(captured[0].signal!.aborted).toBe(false);
    expect(useStore.getState().stakeholder.loading).toBe(true);
    vi.useRealTimers();
  });

  test('the user pressing Stop aborts it and settles the UI', async () => {
    void useStore.getState().askStakeholder('why are purchases failing?');
    await flush();
    useStore.getState().stopStreaming();
    expect(captured[0].signal!.aborted).toBe(true);
    expect(useStore.getState().stakeholder.loading).toBe(false);
    expect(useStore.getState().stakeholder.pendingQuestion).toBe('');
  });

  test('the trail is left standing so the user can see how far it got', async () => {
    void useStore.getState().askStakeholder('why are purchases failing?');
    await flush();
    useStore.setState((s) => ({
      stakeholder: {
        ...s.stakeholder,
        steps: [{ step: 'planning', state: 'start', label: 'Planning the turn' }],
      },
    }));
    useStore.getState().stopStreaming();
    expect(useStore.getState().stakeholder.steps).toHaveLength(1);
  });

  test('a stopped turn does not switch off the spinner of the next one', async () => {
    // The real hazard: the abandoned request finally returns, falls through to
    // its own cleanup, and clears `loading` for a turn it knows nothing about.
    void useStore.getState().askStakeholder('first question');
    await flush();
    const first = release!;
    useStore.getState().stopStreaming();

    void useStore.getState().askStakeholder('second question');
    await flush();
    expect(useStore.getState().stakeholder.loading).toBe(true);

    first();                       // the abandoned turn finally unblocks
    await flush();
    expect(useStore.getState().stakeholder.loading).toBe(true);
  });
});
