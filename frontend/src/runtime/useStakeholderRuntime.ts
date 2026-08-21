'use client';

import {
  useExternalStoreRuntime,
  type AssistantRuntime,
  type AppendMessage,
  type ExternalStoreAdapter,
  type ThreadMessageLike,
} from '@assistant-ui/react';

import { useStore } from '@/store/useStore';
import type { StakeholderMessage } from '@/types/analysis';

/**
 * The bridge between our zustand store and assistant-ui's thread model.
 *
 * Written against @assistant-ui/react 0.15.16's own types, which were read
 * before this file was written -- the adapter shape below (isRunning, messages,
 * convertMessage, onNew) is verified against
 * node_modules/@assistant-ui/core/src/runtimes/external-store/external-store-adapter.ts
 * rather than assumed.
 *
 * ExternalStoreRuntime rather than LocalRuntime because our state already lives
 * in zustand and our backend does not speak the AI SDK protocol. LocalRuntime
 * would mean handing assistant-ui ownership of state it cannot manage.
 */

/** A thread message that still knows which turn it came from. */
export type AnalystThreadMessage = ThreadMessageLike & {
  __source?: StakeholderMessage;
};

/**
 * The id of the in-flight user message. A fixed sentinel rather than a
 * generated id, so that when the answer arrives the pending pair is *replaced*
 * rather than joined by a duplicate -- a doubled final question is the classic
 * symptom of getting this wrong. It cannot collide with a real turn because
 * real ids are backend-generated `ans_...` values.
 */
export const PENDING_ID = '__pending__';

/**
 * Expand each turn into the two messages a thread expects.
 *
 * A StakeholderMessage holds both the question and the answer; assistant-ui
 * wants alternating user/assistant messages. Ids derive from answer_id and
 * never from the array index: an id that changes between renders makes
 * assistant-ui remount every message, and the thread scroll jumps on every
 * store update.
 *
 * Exported separately from the hook, and kept pure, so it can be tested without
 * rendering a runtime.
 */
export function toThreadMessages(
  messages: StakeholderMessage[],
  pending?: { question: string },
): AnalystThreadMessage[] {
  const out: AnalystThreadMessage[] = [];
  for (const m of messages) {
    out.push({
      role: 'user',
      id: `${m.answer_id}:q`,
      content: [{ type: 'text', text: m.question ?? '' }],
    });
    out.push({
      role: 'assistant',
      id: m.answer_id,
      content: [{ type: 'text', text: m.answer ?? '' }],
      // Carried so AnalystMessage can reach analysis, extract_meta, caveats and
      // feedback without a second lookup by id.
      __source: m,
    });
  }
  if (pending?.question) {
    out.push({
      role: 'user',
      id: PENDING_ID,
      content: [{ type: 'text', text: pending.question }],
    });
  }
  return out;
}

/** The text the user actually typed, out of assistant-ui's part array. */
export function textOf(message: Pick<AppendMessage, 'content'> | undefined): string {
  const parts = message?.content ?? [];
  return parts
    .filter((p): p is { type: 'text'; text: string } => p?.type === 'text')
    .map((p) => p.text)
    .join(' ')
    .trim();
}

export type AdapterInput = {
  messages: StakeholderMessage[];
  pendingQuestion: string;
  loading: boolean;
  ask: (text: string) => Promise<void>;
};

/**
 * Pure builder, so `onNew` can be tested without mounting a runtime.
 * `convertMessage` is the identity because `toThreadMessages` has already done
 * the conversion; assistant-ui still needs it present because our message type
 * is not its own ThreadMessage.
 */
export function stakeholderAdapter(
  input: AdapterInput,
): ExternalStoreAdapter<AnalystThreadMessage> {
  return {
    isRunning: input.loading,
    messages: toThreadMessages(input.messages, { question: input.pendingQuestion }),
    convertMessage: (m: AnalystThreadMessage, _idx: number) => m,
    onNew: async (message: AppendMessage) => {
      const text = textOf(message);
      if (!text) return;
      await input.ask(text);
    },
  };
}

export function useStakeholderRuntime(): AssistantRuntime {
  const messages = useStore((s) => s.stakeholder.messages);
  const pendingQuestion = useStore((s) => s.stakeholder.pendingQuestion);
  const loading = useStore((s) => s.stakeholder.loading);
  const ask = useStore((s) => s.askStakeholder);

  return useExternalStoreRuntime(
    stakeholderAdapter({ messages, pendingQuestion, loading, ask }));
}
