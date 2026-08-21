import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, test, vi } from 'vitest';

import { StorylineCheckbox, StorylinePanel, estimateTokens } from '@/components/analyst/StorylinePanel';
import { useStore } from '@/store/useStore';
import type { StakeholderMessage } from '@/types/analysis';

const msg = (over: Partial<StakeholderMessage> = {}): StakeholderMessage => ({
  answer_id: 'a1', question: 'q1', answer: 'ans', answer_mode: 'ANSWERED',
  status: 'ANSWERED', citations: [], caveats: [], facts: [], queries_run: [],
  escalated: false, cost: 0, created_at: '', ...over,
});

const setStakeholder = (over: Record<string, unknown>) => {
  useStore.setState((s) => ({ stakeholder: { ...s.stakeholder, ...over } }));
};

beforeEach(() => {
  useStore.setState({ tenantId: 't1' });
  setStakeholder({
    messages: [], selectedAnswerIds: [], exportError: '',
    reportBuilderOpen: true, activeConversationId: 'c1',
  });
});

describe('estimateTokens', () => {
  test('counts only selected turns', () => {
    const messages = [msg({ answer_id: 'a1', answer: 'x'.repeat(400) }),
                      msg({ answer_id: 'a2', answer: 'y'.repeat(400) })];
    expect(estimateTokens(messages, ['a1'])).toBeLessThan(estimateTokens(messages, ['a1', 'a2']));
  });

  test('is the backend formula: characters over four', () => {
    const messages = [msg({ answer_id: 'a1', question: '', answer: 'z'.repeat(400) })];
    expect(estimateTokens(messages, ['a1'])).toBe(100);
  });
});

describe('StorylineCheckbox', () => {
  test('a turn checkbox toggles it into the selection', async () => {
    const toggleAnswerSelected = vi.fn();
    useStore.setState({ toggleAnswerSelected });
    render(<StorylineCheckbox answerId="a7" />);

    await userEvent.setup().click(screen.getByRole('checkbox'));

    expect(toggleAnswerSelected).toHaveBeenCalledWith('a7');
  });

  test('reflects the current selection', () => {
    setStakeholder({ selectedAnswerIds: ['a7'] });
    render(<StorylineCheckbox answerId="a7" />);
    expect(screen.getByRole('checkbox')).toBeChecked();
  });

  test('is hidden while the report builder is closed', () => {
    setStakeholder({ reportBuilderOpen: false });
    const { container } = render(<StorylineCheckbox answerId="a7" />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('StorylinePanel', () => {
  test('the count reflects in-thread selection', () => {
    setStakeholder({ messages: [msg({ answer_id: 'a1' })], selectedAnswerIds: ['a1'] });
    render(<StorylinePanel />);
    expect(screen.getByText(/1 selected/)).toBeInTheDocument();
  });

  test('the token estimate warns over the threshold', () => {
    setStakeholder({
      messages: [msg({ answer_id: 'a1', answer: 'x'.repeat(4 * 50_001) })],
      selectedAnswerIds: ['a1'],
    });
    render(<StorylinePanel />);
    expect(screen.getByText(/consider selecting fewer turns/)).toBeInTheDocument();
  });

  test('markdown and word are offered', () => {
    render(<StorylinePanel />);
    expect(screen.getByRole('option', { name: /markdown/i })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /word/i })).toBeInTheDocument();
  });

  test('export is disabled with an empty selection', () => {
    render(<StorylinePanel />);
    expect(screen.getByRole('button', { name: /Export/ })).toBeDisabled();
  });

  test('the narrate toggle is off by default and reaches the request', async () => {
    const exportStoryline = vi.fn().mockResolvedValue(undefined);
    useStore.setState({ exportStoryline });
    setStakeholder({ messages: [msg()], selectedAnswerIds: ['a1'] });
    const user = userEvent.setup();
    render(<StorylinePanel />);

    const narrate = screen.getByRole('checkbox', { name: /Write a narrative/i });
    expect(narrate).not.toBeChecked();

    await user.click(screen.getByRole('button', { name: /Export/ }));
    expect(exportStoryline).toHaveBeenLastCalledWith('markdown', false);

    await user.click(narrate);
    await user.click(screen.getByRole('button', { name: /Export/ }));
    expect(exportStoryline).toHaveBeenLastCalledWith('markdown', true);
  });

  test('a backend failure is shown in the alert region', () => {
    // 400 unknown answer_ids, 404 conversation, 503 renderer -- all of them must
    // reach the user rather than looking like a dead button.
    setStakeholder({ exportError: 'docx export unavailable: python-docx not installed' });
    render(<StorylinePanel />);
    expect(screen.getByRole('alert')).toHaveTextContent(/python-docx not installed/);
  });
});
