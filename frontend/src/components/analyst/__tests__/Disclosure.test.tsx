import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, test } from 'vitest';

import { CodeBlock, Disclosure } from '@/components/analyst/Disclosure';

describe('Disclosure', () => {
  test('is collapsed by default and hides its content', () => {
    render(<Disclosure label="SQL"><p>SELECT 1</p></Disclosure>);
    expect(screen.getByRole('button', { name: /SQL/i })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('SELECT 1')).not.toBeInTheDocument();
  });

  test('clicking reveals its content and flips aria-expanded', async () => {
    const user = userEvent.setup();
    render(<Disclosure label="SQL"><p>SELECT 1</p></Disclosure>);
    const toggle = screen.getByRole('button', { name: /SQL/i });

    await user.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('SELECT 1')).toBeInTheDocument();
  });

  test('clicking again collapses it', async () => {
    const user = userEvent.setup();
    render(<Disclosure label="SQL"><p>SELECT 1</p></Disclosure>);
    const toggle = screen.getByRole('button', { name: /SQL/i });

    await user.click(toggle);
    await user.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('SELECT 1')).not.toBeInTheDocument();
  });

  test('the toggle controls the panel it reveals', async () => {
    const user = userEvent.setup();
    render(<Disclosure label="SQL"><p>SELECT 1</p></Disclosure>);
    const toggle = screen.getByRole('button', { name: /SQL/i });
    await user.click(toggle);

    const panelId = toggle.getAttribute('aria-controls');
    expect(panelId).toBeTruthy();
    expect(document.getElementById(panelId!)).toContainElement(screen.getByText('SELECT 1'));
  });

  test('a count badge appears only when there is more than one', () => {
    const { rerender } = render(<Disclosure label="SQL" count={1}><p>x</p></Disclosure>);
    expect(screen.getByRole('button', { name: /SQL/i })).not.toHaveTextContent('1');

    rerender(<Disclosure label="SQL" count={3}><p>x</p></Disclosure>);
    expect(screen.getByRole('button', { name: /SQL/i })).toHaveTextContent('3');
  });
});

describe('CodeBlock', () => {
  test('renders code as text, never as html', () => {
    render(<CodeBlock code={'SELECT 1; <script>alert(1)</script>'} />);
    expect(document.querySelector('script')).toBeNull();
    expect(screen.getByText(/alert\(1\)/)).toBeInTheDocument();
  });

  test('scrolls inside its own container so the page cannot scroll sideways', () => {
    const { container } = render(<CodeBlock code="SELECT * FROM a_very_wide_table" />);
    const pre = container.querySelector('pre');
    expect(pre).toHaveStyle({ overflowX: 'auto' });
  });

  test('shows its label when given one', () => {
    render(<CodeBlock code="SELECT 1" label="Warehouse (Athena)" />);
    expect(screen.getByText('Warehouse (Athena)')).toBeInTheDocument();
  });
});
