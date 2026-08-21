import { afterEach, describe, expect, test, vi } from 'vitest';

import { streamAnswer } from '@/lib/streamAnswer';
import type { StakeholderMessage, StepEvent } from '@/types/analysis';

/**
 * A Response whose body yields exactly these chunks. Hand-rolled rather than
 * built from a real ReadableStream so the chunk boundaries are exactly where
 * the test puts them -- which is the entire point, since a boundary landing
 * mid-JSON is where this parser would actually break.
 */
function streamOf(...chunks: string[]): Response {
  const encoder = new TextEncoder();
  let i = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: async () =>
          i < chunks.length
            ? { done: false, value: encoder.encode(chunks[i++]) }
            : { done: true, value: undefined },
        cancel: async () => undefined,
        releaseLock: () => undefined,
      }),
    },
  } as unknown as Response;
}

function handlers() {
  const steps: StepEvent[] = [];
  const answers: unknown[] = [];
  const errors: string[] = [];
  return {
    steps, answers, errors,
    h: {
      onStep: (e: StepEvent) => steps.push(e),
      onAnswer: (m: StakeholderMessage) => answers.push(m),
      onError: (d: string) => errors.push(d),
    },
  };
}

afterEach(() => vi.restoreAllMocks());

describe('streamAnswer', () => {
  test('parses step events then the answer', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamOf(
      'event: step\ndata: {"step":"planning","state":"done","label":"Planning"}\n\n',
      'event: answer\ndata: {"answer_id":"a1","answer":"hi"}\n\n'));
    const { steps, answers, errors, h } = handlers();

    await streamAnswer('t', 'q', 'c1', h);

    expect(steps).toHaveLength(1);
    expect(steps[0].step).toBe('planning');
    expect((answers[0] as { answer_id: string }).answer_id).toBe('a1');
    expect(errors).toEqual([]);
  });

  test('a frame split across chunk boundaries is reassembled', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamOf(
      'event: answer\ndata: {"answer_id":"a', '1","answer":"hi"}\n\n'));
    const { answers, errors, h } = handlers();

    await streamAnswer('t', 'q', 'c1', h);

    expect(answers).toHaveLength(1);
    expect((answers[0] as { answer_id: string }).answer_id).toBe('a1');
    expect(errors).toEqual([]);
  });

  test('a boundary between the event line and the data line is handled', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamOf(
      'event: ans', 'wer\nda', 'ta: {"answer_id":"a1"}\n', '\n'));
    const { answers, h } = handlers();

    await streamAnswer('t', 'q', 'c1', h);

    expect(answers).toHaveLength(1);
  });

  test('a multi-line answer survives, because newlines are escaped in the JSON', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamOf(
      'event: answer\ndata: {"answer_id":"a1","answer":"one\\ntwo"}\n\n'));
    const { answers, h } = handlers();

    await streamAnswer('t', 'q', 'c1', h);

    expect((answers[0] as { answer: string }).answer).toBe('one\ntwo');
  });

  test('an error event reports the detail and never calls onAnswer', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamOf(
      'event: step\ndata: {"step":"planning","state":"start","label":"Planning"}\n\n',
      'event: error\ndata: {"detail":"the warehouse fell over"}\n\n'));
    const { answers, errors, h } = handlers();

    await streamAnswer('t', 'q', 'c1', h);

    expect(answers).toEqual([]);
    expect(errors).toEqual(['the warehouse fell over']);
  });

  test('a stream that ends without an answer is an error', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamOf(
      'event: step\ndata: {"step":"planning","state":"start","label":"Planning"}\n\n'));
    const { answers, errors, h } = handlers();

    await streamAnswer('t', 'q', 'c1', h);

    expect(answers).toEqual([]);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatch(/closed before an answer/i);
  });

  test('a non-ok response reports the backend detail', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false, status: 404, statusText: 'Not Found',
      json: async () => ({ detail: 'Unknown tenant nope' }),
    } as unknown as Response);
    const { errors, h } = handlers();

    await streamAnswer('t', 'q', 'c1', h);

    expect(errors).toEqual(['Unknown tenant nope']);
  });

  test('falls back to the blocking route when the body is not readable', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce({ ok: true, status: 200, body: null } as unknown as Response)
      .mockResolvedValueOnce({
        ok: true, status: 200,
        json: async () => ({ answer_id: 'a1', answer: 'via fallback' }),
      } as unknown as Response);
    const { answers, errors, h } = handlers();

    await streamAnswer('t', 'q', 'c1', h);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[1][0]).toContain('/answer');
    expect(fetchMock.mock.calls[1][0]).not.toContain('/stream');
    expect((answers[0] as { answer: string }).answer).toBe('via fallback');
    expect(errors).toEqual([]);
  });

  test('abort stops reading without calling onError', async () => {
    const controller = new AbortController();
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
      controller.abort();
      const err = new Error('aborted');
      err.name = 'AbortError';
      throw err;
    });
    const { answers, errors, h } = handlers();

    await streamAnswer('t', 'q', 'c1', h, controller.signal);

    expect(answers).toEqual([]);
    expect(errors).toEqual([]);
  });

  test('the question travels in the body, never in the url', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(streamOf(
      'event: answer\ndata: {"answer_id":"a1"}\n\n'));
    const { h } = handlers();

    await streamAnswer('t', 'what is revenue by country?', 'c1', h);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).not.toContain('revenue');
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      question: 'what is revenue by country?', conversation_id: 'c1',
    });
  });
});
