import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, test, vi } from 'vitest';

import { AnalystMessageBody, AnswerProse, Caveats } from '@/components/analyst/AnalystMessage';
import { useStore } from '@/store/useStore';
import type { StakeholderMessage } from '@/types/analysis';

const msg = (over: Partial<StakeholderMessage> = {}): StakeholderMessage => ({
  answer_id: 'a1', question: 'q1', answer: 'ans', answer_mode: 'ADAPTED_APPROVED_QUERY',
  status: 'ANSWERED', citations: [], caveats: [], facts: [], queries_run: [],
  escalated: false, cost: 0, created_at: '', ...over,
});

describe('AnswerProse', () => {
  test('markdown in the answer is rendered, not shown as asterisks', () => {
    // This has never worked: the answer was always markdown and was always
    // rendered into a plain <p>.
    const { container } = render(<AnswerProse text="revenue is **up** sharply" />);
    expect(container.querySelector('strong')).toHaveTextContent('up');
    expect(container.textContent).not.toContain('**');
  });

  test('lists render as lists', () => {
    const { container } = render(<AnswerProse text={'- one\n- two'} />);
    expect(container.querySelectorAll('li')).toHaveLength(2);
  });

  test('raw html in an answer is escaped, not executed', () => {
    const { container } = render(
      <AnswerProse text={'<img src=x onerror="alert(1)"> and <script>alert(2)</script>'} />);
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
  });
});

describe('Caveats', () => {
  test('an undefined metric is visible in the body, not hidden behind a click', () => {
    // Plan A's uncertainty mechanism is defeated by a UI that files this under
    // Methodology, so it is asserted to be plain visible text.
    render(<Caveats caveats={["'churn' is not a defined metric for this company"]} />);
    expect(screen.getByText(/not a defined metric/)).toBeVisible();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  test('renders nothing when there are none', () => {
    const { container } = render(<Caveats caveats={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('AnalystMessageBody', () => {
  beforeEach(() => {
    useStore.setState({ tenantId: 't1' });
  });

  test('renders the answer prose and the mode pill', () => {
    render(<AnalystMessageBody message={msg({ answer: 'Revenue is up.' })} />);
    expect(screen.getByText('Revenue is up.')).toBeInTheDocument();
    expect(screen.getByText('ADAPTED_APPROVED_QUERY')).toBeInTheDocument();
  });

  test('feedback buttons keep their accessible names and pressed state', () => {
    render(<AnalystMessageBody message={msg({ feedback: 'up' })} />);
    expect(screen.getByRole('button', { name: 'Good answer' }))
      .toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Bad answer' }))
      .toHaveAttribute('aria-pressed', 'false');
  });

  test('clicking thumbs down submits feedback for that answer', async () => {
    const submitFeedback = vi.fn();
    useStore.setState({ submitFeedback });
    const user = userEvent.setup();
    render(<AnalystMessageBody message={msg({ answer_id: 'a7' })} />);

    await user.click(screen.getByRole('button', { name: 'Bad answer' }));

    expect(submitFeedback).toHaveBeenCalledWith('a7', 'down');
  });

  test('a turn with no answer_id offers no feedback buttons', () => {
    render(<AnalystMessageBody message={msg({ answer_id: '' })} />);
    expect(screen.queryByRole('button', { name: 'Good answer' })).not.toBeInTheDocument();
  });

  test('a pre-Plan-A turn with no analysis still renders', () => {
    render(<AnalystMessageBody message={msg({ answer: 'old answer', analysis: undefined })} />);
    expect(screen.getByText('old answer')).toBeInTheDocument();
  });
});
