import { describe, expect, test, vi } from 'vitest';

import { PENDING_ID, stakeholderAdapter, textOf, toThreadMessages } from '@/runtime/useStakeholderRuntime';
import type { StakeholderMessage } from '@/types/analysis';

const msg = (over: Partial<StakeholderMessage>): StakeholderMessage => ({
  answer_id: 'a1', question: 'q1', answer: 'ans', answer_mode: 'ANSWERED',
  status: 'ANSWERED', citations: [], caveats: [], facts: [], queries_run: [],
  escalated: false, cost: 0, created_at: '', ...over,
});

describe('toThreadMessages', () => {
  test('each stakeholder message expands into a user and an assistant message', () => {
    const out = toThreadMessages([msg({ answer_id: 'a1', question: 'q1', answer: 'ans' })]);
    expect(out.map((m) => m.role)).toEqual(['user', 'assistant']);
    expect(out[1].id).toBe('a1');
  });

  test('ids are stable across calls and unique', () => {
    const msgs = [msg({ answer_id: 'a1' }), msg({ answer_id: 'a2' })];
    const ids = toThreadMessages(msgs).map((m) => m.id);
    expect(new Set(ids).size).toBe(ids.length);
    // Derived from answer_id, never from the array index -- an id that changes
    // between renders makes assistant-ui remount every message and the thread
    // scroll jumps on each store update.
    expect(toThreadMessages(msgs).map((m) => m.id)).toEqual(ids);
  });

  test('a pending question appears once and is replaced, not duplicated', () => {
    const pending = toThreadMessages([], { question: 'q1' });
    expect(pending).toHaveLength(1);
    expect(pending[0].id).toBe(PENDING_ID);

    const settled = toThreadMessages([msg({ answer_id: 'a1', question: 'q1' })]);
    expect(settled.filter((m) => m.role === 'user')).toHaveLength(1);
    expect(settled.some((m) => m.id === PENDING_ID)).toBe(false);
  });

  test('the pending sentinel cannot collide with a real turn', () => {
    const out = toThreadMessages([msg({ answer_id: 'a1' })], { question: 'q2' });
    const ids = out.map((m) => m.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids.filter((i) => i === PENDING_ID)).toHaveLength(1);
  });

  test('the assistant message carries the raw stakeholder message', () => {
    const out = toThreadMessages([
      msg({ answer_id: 'a1', analysis: { datasets_used: ['df_1'] } }),
    ]);
    expect(out[1].__source?.analysis?.datasets_used).toEqual(['df_1']);
  });

  test('the user message carries no source payload', () => {
    const out = toThreadMessages([msg({ answer_id: 'a1' })]);
    expect(out[0].__source).toBeUndefined();
  });

  test('an empty answer still produces a message rather than vanishing', () => {
    const out = toThreadMessages([msg({ answer_id: 'a1', answer: '' })]);
    expect(out).toHaveLength(2);
    expect(out[1].id).toBe('a1');
  });
});

describe('textOf', () => {
  test('reads the text parts of a composed message', () => {
    expect(textOf({ content: [{ type: 'text', text: 'hello' }] })).toBe('hello');
  });

  test('joins multiple text parts and ignores non-text ones', () => {
    expect(textOf({
      content: [
        { type: 'text', text: 'a' },
        { type: 'image', image: 'x' },
        { type: 'text', text: 'b' },
      ],
    })).toBe('a b');
  });

  test('a message with no text is the empty string, not a crash', () => {
    expect(textOf({ content: [] })).toBe('');
    expect(textOf(undefined)).toBe('');
  });
});

describe('stakeholderAdapter', () => {
  test('submitting through the adapter calls askStakeholder with the text', async () => {
    const ask = vi.fn().mockResolvedValue(undefined);
    const adapter = stakeholderAdapter({
      messages: [], pendingQuestion: '', loading: false, ask,
    });

    await adapter.onNew({ content: [{ type: 'text', text: 'why did US fall?' }] } as never);

    expect(ask).toHaveBeenCalledWith('why did US fall?');
  });

  test('an empty submission is not sent', async () => {
    const ask = vi.fn().mockResolvedValue(undefined);
    const adapter = stakeholderAdapter({
      messages: [], pendingQuestion: '', loading: false, ask,
    });

    await adapter.onNew({ content: [] } as never);

    expect(ask).not.toHaveBeenCalled();
  });

  test('isRunning follows the store loading flag', () => {
    const base = { messages: [], pendingQuestion: '', ask: vi.fn() };
    expect(stakeholderAdapter({ ...base, loading: true }).isRunning).toBe(true);
    expect(stakeholderAdapter({ ...base, loading: false }).isRunning).toBe(false);
  });

  test('the adapter exposes the converted thread, including the pending turn', () => {
    const adapter = stakeholderAdapter({
      messages: [msg({ answer_id: 'a1' })], pendingQuestion: 'q2',
      loading: true, ask: vi.fn(),
    });
    expect(adapter.messages?.map((m) => m.role)).toEqual(['user', 'assistant', 'user']);
  });

  test('convertMessage is identity, because conversion already happened', () => {
    const adapter = stakeholderAdapter({
      messages: [], pendingQuestion: '', loading: false, ask: vi.fn(),
    });
    const m = { role: 'user' as const, id: 'x', content: [{ type: 'text' as const, text: 'hi' }] };
    // The converter takes (message, idx) in 0.15.16.
    expect(adapter.convertMessage(m, 0)).toBe(m);
  });
});
