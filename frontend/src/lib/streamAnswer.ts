import { apiUrl } from '@/lib/api';
import type { StakeholderMessage, StepEvent } from '@/types/analysis';

export type StreamHandlers = {
  onStep: (e: StepEvent) => void;
  onAnswer: (m: StakeholderMessage) => void;
  onError: (detail: string) => void;
};

const CLOSED_EARLY = 'the connection closed before an answer arrived';

function isAbort(err: unknown, signal?: AbortSignal): boolean {
  return signal?.aborted === true
    || (err instanceof Error && err.name === 'AbortError');
}

async function detailOf(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (body && typeof body === 'object' && 'detail' in body) {
      const d = (body as { detail: unknown }).detail;
      return typeof d === 'string' ? d : JSON.stringify(d);
    }
  } catch {
    // A non-JSON error body is not itself an error worth reporting; fall
    // through to the status text, which is at least true.
  }
  return res.statusText || `request failed with ${res.status}`;
}

/**
 * POST the question and read the SSE-shaped response body.
 *
 * POST rather than EventSource is the whole reason this function exists:
 * EventSource is GET-only, so the question would end up in the URL and in every
 * proxy log. That costs about fifteen lines of parsing, which is this.
 *
 * Three terminal conditions are handled distinctly, because a client that
 * cannot tell them apart shows a spinner forever:
 *   - an `answer` event      -> success
 *   - an `error` event       -> failure, with the backend's detail
 *   - the stream just ending -> failure, and we say so
 */
export async function streamAnswer(
  tenantId: string,
  question: string,
  conversationId: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const { onStep, onAnswer, onError } = handlers;
  const payload = { question, conversation_id: conversationId };

  try {
    const res = await fetch(
      apiUrl(`/stakeholder/${encodeURIComponent(tenantId)}/answer/stream`),
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        signal,
      });

    if (!res.ok) {
      onError(await detailOf(res));
      return;
    }

    // The blocking route still exists precisely so streaming can be optional.
    // An environment without a readable body gets a working answer, not an
    // error message about streams.
    const reader = res.body?.getReader?.();
    if (!reader) {
      const fallback = await fetch(
        apiUrl(`/stakeholder/${encodeURIComponent(tenantId)}/answer`),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          signal,
        });
      if (!fallback.ok) {
        onError(await detailOf(fallback));
        return;
      }
      onAnswer(await fallback.json());
      return;
    }

    const decoder = new TextDecoder();
    let buffer = '';
    let settled = false;

    // A chunk boundary lands anywhere, including inside a JSON payload, so a
    // partial frame stays in the buffer until its terminator arrives. Never
    // JSON.parse a fragment.
    const consume = (frame: string): boolean => {
      let event = '';
      let data = '';
      for (const line of frame.split('\n')) {
        if (line.startsWith('event: ')) event = line.slice(7).trim();
        else if (line.startsWith('data: ')) data = line.slice(6);
      }
      if (!event || !data) return false;

      let parsed: unknown;
      try {
        parsed = JSON.parse(data);
      } catch {
        return false;
      }

      if (event === 'step') {
        onStep(parsed as StepEvent);
      } else if (event === 'answer') {
        settled = true;
        onAnswer(parsed as StakeholderMessage);
      } else if (event === 'error') {
        settled = true;
        const d = (parsed as { detail?: unknown }).detail;
        onError(typeof d === 'string' ? d : 'the analyst failed mid-answer');
        return true;
      }
      return false;
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split = buffer.indexOf('\n\n');
      while (split !== -1) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        if (consume(frame)) return;
        split = buffer.indexOf('\n\n');
      }
    }

    // A final frame the server did not terminate is still a frame.
    if (buffer.trim() && consume(buffer)) return;
    if (!settled) onError(CLOSED_EARLY);
  } catch (err) {
    // An aborted request is the user changing their mind, not a failure to
    // report at them.
    if (isAbort(err, signal)) return;
    console.error(err);
    onError(err instanceof Error ? err.message : 'the request failed');
  }
}
