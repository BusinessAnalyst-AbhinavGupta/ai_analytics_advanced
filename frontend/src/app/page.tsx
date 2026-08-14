"use client";

import { StakeholderChat } from '@/components/StakeholderChat';

export default function Home() {
  return (
    <main style={{ padding: '2rem' }}>
      <div className="glass-panel" style={{ padding: '1.5rem' }}>
        <h1 style={{ marginBottom: '1rem' }}>Stakeholder Q&A</h1>
        <StakeholderChat />
      </div>
    </main>
  );
}
