'use client';

import { ThumbsDown, ThumbsUp } from 'lucide-react';
import Markdown from 'react-markdown';

import { useStore } from '@/store/useStore';
import type { StakeholderMessage } from '@/types/analysis';

/**
 * The answer prose.
 *
 * The answer text has always been markdown and was always rendered into a plain
 * <p>, so every bold and every list in every answer has been shown to users as
 * literal asterisks. This is the fix.
 *
 * react-markdown directly rather than @assistant-ui/react-markdown's
 * MarkdownTextPrimitive, which only works inside assistant-ui's message-part
 * context and would make this untestable in isolation. react-markdown is what
 * that package wraps, and it does not render raw HTML unless rehype-raw is
 * added -- which it is not, deliberately. This is model output, and one day it
 * will contain a <script>.
 */
export function AnswerProse({ text }: { text: string }) {
  return (
    <div style={{ color: 'var(--text-secondary)', lineHeight: 1.6 }}>
      <Markdown>{text}</Markdown>
    </div>
  );
}

/**
 * Uncertainty belongs above the fold.
 *
 * Plan A's entire uncertainty mechanism is defeated by a UI that files "this is
 * not a defined metric" one click away under Methodology. AnalysisDisclosures
 * renders the detail; this renders the warning, in the message body, where it
 * cannot be missed.
 */
export function Caveats({ caveats }: { caveats: string[] }) {
  if (!caveats?.length) return null;
  return (
    <ul
      aria-label="Caveats"
      style={{
        margin: '0.75rem 0 0', padding: '0.6rem 0.75rem 0.6rem 1.6rem',
        borderLeft: '3px solid var(--error)', background: 'rgba(239,68,68,0.06)',
        borderRadius: '4px', fontSize: '0.85rem', color: 'var(--text-secondary)',
      }}
    >
      {caveats.map((c, i) => <li key={i}>{c}</li>)}
    </ul>
  );
}

function FeedbackButtons({ message }: { message: StakeholderMessage }) {
  const submitFeedback = useStore((s) => s.submitFeedback);
  if (!message.answer_id) return null;
  return (
    <div style={{ display: 'flex', gap: '0.5rem', marginTop: '1rem' }}>
      <button
        title="Good answer"
        aria-label="Good answer"
        aria-pressed={message.feedback === 'up'}
        onClick={() => submitFeedback(message.answer_id, 'up')}
        style={{
          background: message.feedback === 'up' ? 'rgba(34,197,94,0.15)' : 'none',
          border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px',
          padding: '0.3rem 0.5rem', cursor: 'pointer', display: 'flex',
          color: message.feedback === 'up' ? 'var(--success)' : 'var(--text-muted)',
        }}
      >
        <ThumbsUp size={14} />
      </button>
      <button
        title="Bad answer"
        aria-label="Bad answer"
        aria-pressed={message.feedback === 'down'}
        onClick={() => submitFeedback(message.answer_id, 'down')}
        style={{
          background: message.feedback === 'down' ? 'rgba(239,68,68,0.15)' : 'none',
          border: '1px solid rgba(255,255,255,0.1)', borderRadius: '6px',
          padding: '0.3rem 0.5rem', cursor: 'pointer', display: 'flex',
          color: message.feedback === 'down' ? 'var(--error)' : 'var(--text-muted)',
        }}
      >
        <ThumbsDown size={14} />
      </button>
    </div>
  );
}

/**
 * Everything below an answer that assistant-ui has no opinion about.
 *
 * Pure in its props so it can be tested without mounting a runtime; the thread
 * wraps it with the message pulled off assistant-ui's context.
 */
export function AnalystMessageBody({ message }: { message: StakeholderMessage }) {
  return (
    <div
      style={{
        padding: '1.25rem', background: 'rgba(0,0,0,0.2)', borderRadius: '12px',
        border: '1px solid rgba(255,255,255,0.05)',
      }}
    >
      {message.answer_mode && (
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'center',
          marginBottom: '0.75rem',
        }}>
          <span style={{
            fontSize: '0.8rem', padding: '0.2rem 0.6rem', borderRadius: '12px',
            background: 'rgba(255,255,255,0.1)', color: 'var(--text-secondary)',
          }}>
            {message.answer_mode}
          </span>
        </div>
      )}

      <AnswerProse text={message.answer ?? ''} />
      <Caveats caveats={message.caveats ?? []} />
      <FeedbackButtons message={message} />
    </div>
  );
}
