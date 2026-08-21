'use client';

import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  ThreadPrimitive,
  useAuiState,
} from '@assistant-ui/react';

import { AnalystMessageBody } from '@/components/analyst/AnalystMessage';
import { ConversationSidebar } from '@/components/analyst/ConversationSidebar';
import { StepTrail } from '@/components/analyst/StepTrail';
import { StorylinePanel } from '@/components/analyst/StorylinePanel';
import { useStakeholderRuntime } from '@/runtime/useStakeholderRuntime';
import { useStore } from '@/store/useStore';

/**
 * The chat frame is bought; the analytical layer is built.
 *
 * Thread scroll, the composer, message grouping and keyboard behaviour are
 * assistant-ui's. What is ours is everything below the answer prose -- the step
 * trail, the provenance disclosures, the chart, and the storyline selection --
 * because those are the only reason this is not a chat wrapper.
 */

function UserMessage() {
  // useAuiState, not useMessage: the latter does not exist in 0.15.16.
  const content = useAuiState((s) => s.message.content);
  const text = content
    .map((part) => (part.type === 'text' ? part.text : ''))
    .join('')
    .trim();
  return (
    <p style={{ color: 'var(--text-primary)', fontWeight: 600, margin: '1.5rem 0 0.5rem' }}>
      {text}
    </p>
  );
}

/**
 * The assistant slot.
 *
 * The turn is looked up by message id rather than pulled off assistant-ui's
 * internal symbol store: ids are answer_ids by construction (see
 * toThreadMessages), and a plain lookup does not depend on library internals
 * that move between versions. A turn that cannot be found still renders, so a
 * mismatch degrades to an empty body rather than a crashed thread.
 */
function AssistantMessage() {
  const id = useAuiState((s) => s.message.id);
  const source = useStore(
    (s) => s.stakeholder.messages.find((m) => m.answer_id === id));

  // While a turn is in flight assistant-ui renders a placeholder assistant
  // message that has no turn behind it yet. Rendering the body for it produced
  // an empty answer bubble carrying thumbs up/down -- feedback controls for an
  // answer that does not exist. The StepTrail is what reports progress, so this
  // renders nothing until there is a real turn to show.
  if (!source) return null;

  return <AnalystMessageBody message={source} />;
}

function Composer() {
  const loading = useStore((s) => s.stakeholder.loading);
  return (
    <ComposerPrimitive.Root
      style={{
        display: 'flex', gap: '1rem', padding: '1.5rem', alignItems: 'flex-end',
        borderTop: '1px solid rgba(255,255,255,0.08)',
        // Without flexShrink the composer is a flex child with no height of its
        // own, so the textarea and the send button stretch down the whole
        // column. The old chat used an <input>, which has an intrinsic height;
        // assistant-ui's Input is a textarea, which does not.
        flexShrink: 0,
      }}
    >
      {/* A react-textarea-autosize under the hood, so height comes from
          minRows/maxRows rather than CSS -- min/maxHeight are not even in its
          style type.

          The wrapper is load-bearing. autosize measures a hidden clone, and if
          the textarea itself carries `flex: 1` with no resolvable width the
          clone measures at zero width, scrollHeight explodes, and every empty
          composer renders clamped at maxRows. Putting flex on the wrapper and
          width:100% on the textarea gives the clone a width to measure against. */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <ComposerPrimitive.Input
          minRows={1}
          maxRows={6}
          placeholder="E.g. What is our revenue over time?"
          style={{
            width: '100%', padding: '0.75rem 1rem', background: 'rgba(0,0,0,0.2)',
            border: '1px solid rgba(255,255,255,0.1)', borderRadius: '8px',
            color: '#fff', resize: 'none', display: 'block',
            fontFamily: 'inherit', fontSize: '1rem', lineHeight: 1.4,
            // The first-paint height, before autosize has measured. Without it
            // the composer can briefly render at three rows and snap to one,
            // because the clone measurement runs against a not-yet-settled
            // layout. autosize replaces this with its own `height: ...px
            // !important` the moment it measures, so it only governs the very
            // first frame. Typed as a number (px) by TextareaAutosizeProps:
            // 48 = one 22.4px row plus 24px of vertical padding.
            height: 48,
          }}
        />
      </div>
      <ComposerPrimitive.Send
        style={{
          background: 'var(--accent-primary)', padding: '0.75rem 1.5rem',
          borderRadius: '8px', border: 'none', color: '#fff', cursor: 'pointer',
          fontWeight: 600, flexShrink: 0, height: '3rem',
        }}
      >
        {loading ? 'Asking...' : 'Ask'}
      </ComposerPrimitive.Send>
    </ComposerPrimitive.Root>
  );
}

export function AnalystThread() {
  const runtime = useStakeholderRuntime();
  const reportBuilderOpen = useStore((s) => s.stakeholder.reportBuilderOpen);
  const streamError = useStore((s) => s.stakeholder.streamError);
  const steps = useStore((s) => s.stakeholder.steps);
  const loading = useStore((s) => s.stakeholder.loading);
  const toggleReportBuilder = useStore((s) => s.toggleReportBuilder);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <div style={{ display: 'flex', height: 'calc(100vh - 4rem)' }}>
        <ConversationSidebar />
        <ThreadPrimitive.Root style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <div style={{ display: 'flex', justifyContent: 'flex-end', padding: '0.75rem 1.5rem 0' }}>
            <button
              onClick={toggleReportBuilder}
              style={{
                fontSize: '0.85rem', padding: '0.4rem 0.8rem', background: 'none',
                border: '1px solid rgba(255,255,255,0.15)', borderRadius: '6px',
                color: 'var(--text-secondary)', cursor: 'pointer',
              }}
            >
              {reportBuilderOpen ? 'Hide' : 'Report Builder'}
            </button>
          </div>

          {/* minHeight: 0 is required: a flex child defaults to min-content
              height, which stops it shrinking and makes the scroll container
              overflow its parent instead of scrolling inside it. */}
          <ThreadPrimitive.Viewport
            style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '1.5rem' }}
          >
            <ThreadPrimitive.Empty>
              <p style={{ color: 'var(--text-secondary)' }}>
                Ask a question in plain English. The AI will query the company brain,
                refresh approved metrics, or safely generate an answer.
              </p>
            </ThreadPrimitive.Empty>

            <ThreadPrimitive.Messages
              components={{ UserMessage, AssistantMessage }}
            />

            {/* The live trail for the turn in flight. It sits below the thread
                because it describes the answer being written, not one already
                written -- historical turns collapse their own trail instead. */}
            <StepTrail steps={steps} running={loading} />

            {/* A failed turn has to say so. Without this the composer just sits
                there and a dead backend is indistinguishable from a slow one. */}
            {streamError && (
              <div role="alert" style={{ marginTop: '1rem', color: 'var(--error)', fontSize: '0.85rem' }}>
                That question failed: {streamError}
              </div>
            )}
          </ThreadPrimitive.Viewport>

          <Composer />
        </ThreadPrimitive.Root>
        {reportBuilderOpen && <StorylinePanel />}
      </div>
    </AssistantRuntimeProvider>
  );
}
