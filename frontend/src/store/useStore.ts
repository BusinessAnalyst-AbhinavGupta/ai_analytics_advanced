import { create } from 'zustand';

import { apiUrl } from '@/lib/api';
import { streamAnswer } from '@/lib/streamAnswer';
import type { StakeholderMessage, StepEvent } from '@/types/analysis';

// Re-exported so existing importers keep working; the definition now lives in
// types/analysis.ts, which is also what streamAnswer produces.
export type { StakeholderMessage } from '@/types/analysis';

export type ConversationSummary = {
  id: string; title: string; starred: boolean;
  created_at: string; updated_at: string; message_count: number;
};

interface AppState {
  // Global
  tenantId: string;
  setTenantId: (id: string) => void;

  // Stakeholder Q&A
  stakeholder: {
    question: string;
    loading: boolean;
    conversations: ConversationSummary[];
    conversationsLoading: boolean;
    activeConversationId: string;
    messages: StakeholderMessage[];
    // The live pipeline trail for the turn in flight, cleared at the start of
    // every ask. See StepTrail.
    steps: StepEvent[];
    // The question currently in flight, so the thread can show it before an
    // answer_id exists.
    pendingQuestion: string;
    // A turn that failed. Without this the UI spins forever on a dead backend.
    streamError: string;
    reportBuilderOpen: boolean;
    selectedAnswerIds: string[];
    exportError: string;
  };
  setStakeholder: (data: Partial<AppState['stakeholder']>) => void;
  fetchConversations: () => Promise<void>;
  loadConversation: (id: string) => Promise<void>;
  startNewConversation: () => void;
  askStakeholder: (text: string) => Promise<void>;
  renameConversation: (id: string, title: string) => Promise<void>;
  starConversation: (id: string, starred: boolean) => Promise<void>;
  deleteConversation: (id: string) => Promise<void>;
  submitFeedback: (answerId: string, rating: 'up' | 'down') => Promise<void>;
  toggleReportBuilder: () => void;
  toggleAnswerSelected: (answerId: string) => void;
  selectAllAnswers: () => void;
  clearSelectedAnswers: () => void;
  exportStoryline: (format: 'markdown' | 'docx') => Promise<void>;

  // Junior Activity
  junior: {
    logs: any[];
    isConnected: boolean;
  };
  setJunior: (data: Partial<AppState['junior']>) => void;
  appendJuniorLog: (log: any) => void;

  // Pipeline / Catalog
  pipeline: {
    catalog: any;
    loading: boolean;
    refreshing: boolean;
    hasFetched: boolean;
  };
  setPipeline: (data: Partial<AppState['pipeline']>) => void;

  // Triage & Brain
  triage: {
    activeTab: string;
    knowledgeFilterStatus: string;
    seniorQueue: any[];
    triageQueue: any[];
    loading: boolean;
    hasFetched: boolean;
  };
  setTriage: (data: Partial<AppState['triage']>) => void;
  approveKnowledgeNodes: (ids: string[]) => Promise<void>;
  rejectKnowledgeNodes: (ids: string[]) => Promise<void>;
  reviewSeniorRun: (runId: string, action: 'approve' | 'reject') => Promise<void>;

  // Deep Research
  research: {
    file: File | null;
    query: string;
    results: any[];
    loading: boolean;
    uploadStatus: string;
  };
  setResearch: (data: Partial<AppState['research']>) => void;

  // KPIs
  kpis: {
    kpisList: any[];
    creatableKpis: any[];
    loading: boolean;
    expandedRow: string | null;
    trendingData: Record<string, any>;
    editingSql: Record<string, string>;
    showAddKpi: boolean;
    newKpi: any;
    hasFetched: boolean;
  };
  setKpis: (data: Partial<AppState['kpis']>) => void;

  // Governance
  governance: {
    retentionDays: number;
    piiDetection: boolean;
  };
  setGovernance: (data: Partial<AppState['governance']>) => void;

  // Observability
  observability: {
    logs: any[];
    status: any;
    loading: boolean;
    purging: boolean;
    triggering: boolean;
    hasFetched: boolean;
  };
  setObservability: (data: Partial<AppState['observability']>) => void;

  // Config
  config: {
    profile: any;
    targets: any[];
    aiConfig: any | null;
    loading: boolean;
    saving: boolean;
    savingProfile: boolean;
    showAddTarget: boolean;
    newTarget: any;
    hasFetched: boolean;
  };
  setConfig: (data: Partial<AppState['config']>) => void;
}

export const useStore = create<AppState>((set) => ({
  // No tenant is assumed real -- the Sidebar fetches /tenants on load and
  // sets this to an actual tenant id. A stale hardcoded value here 404s
  // every request silently (fetch() doesn't throw on HTTP error status),
  // which is indistinguishable from "it worked but found nothing".
  tenantId: '',
  setTenantId: (id) => set({ tenantId: id }),

  stakeholder: {
    question: '', loading: false, conversations: [], conversationsLoading: false,
    activeConversationId: '', messages: [],
    steps: [], pendingQuestion: '', streamError: '',
    reportBuilderOpen: false, selectedAnswerIds: [], exportError: '',
  },
  setStakeholder: (data) => set((state) => ({ stakeholder: { ...state.stakeholder, ...data } })),

  fetchConversations: async () => {
    const { tenantId } = useStore.getState();
    if (!tenantId) return;
    set((state) => ({ stakeholder: { ...state.stakeholder, conversationsLoading: true } }));
    try {
      const res = await fetch(apiUrl(`/stakeholder/${tenantId}/conversations`));
      const data = await res.json();
      set((state) => ({ stakeholder: { ...state.stakeholder, conversations: Array.isArray(data) ? data : [] } }));
    } catch (e) {
      console.error(e);
    }
    set((state) => ({ stakeholder: { ...state.stakeholder, conversationsLoading: false } }));
  },

  loadConversation: async (id) => {
    const { tenantId } = useStore.getState();
    if (!tenantId || !id) return;
    try {
      const res = await fetch(apiUrl(`/stakeholder/${tenantId}/conversations/${id}`));
      if (!res.ok) return;
      const data = await res.json();
      set((state) => ({
        stakeholder: {
          ...state.stakeholder, activeConversationId: id, messages: data.messages || [],
          selectedAnswerIds: [], steps: [], pendingQuestion: '', streamError: '',
        },
      }));
    } catch (e) {
      console.error(e);
    }
  },

  startNewConversation: () => {
    set((state) => ({
      stakeholder: {
        ...state.stakeholder, activeConversationId: '', messages: [], question: '',
        selectedAnswerIds: [], steps: [], pendingQuestion: '', streamError: '',
      },
    }));
  },

  askStakeholder: async (text) => {
    const { tenantId, stakeholder } = useStore.getState();
    const queryText = text || stakeholder.question;
    if (!queryText || !tenantId) return;
    // Clear the trail at the start of every ask. A stale trail from the previous
    // question sitting beside a new answer is worse than showing no trail at all.
    set((state) => ({
      stakeholder: {
        ...state.stakeholder, loading: true, steps: [], streamError: '',
        pendingQuestion: queryText,
      },
    }));
    try {
      await streamAnswer(tenantId, queryText, stakeholder.activeConversationId, {
        onStep: (e) => set((state) => ({
          stakeholder: { ...state.stakeholder, steps: [...state.stakeholder.steps, e] },
        })),
        onAnswer: (data) => set((state) => ({
          stakeholder: {
            ...state.stakeholder,
            question: '',
            pendingQuestion: '',
            activeConversationId:
              data.conversation_id || state.stakeholder.activeConversationId,
            messages: [...state.stakeholder.messages, data],
          },
        })),
        onError: (detail) => set((state) => ({
          stakeholder: { ...state.stakeholder, pendingQuestion: '', streamError: detail },
        })),
      });
      await useStore.getState().fetchConversations();
    } catch (e) {
      console.error(e);
    }
    set((state) => ({ stakeholder: { ...state.stakeholder, loading: false } }));
  },

  renameConversation: async (id, title) => {
    const { tenantId } = useStore.getState();
    try {
      await fetch(apiUrl(`/stakeholder/${tenantId}/conversations/${id}`), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      });
      await useStore.getState().fetchConversations();
    } catch (e) {
      console.error(e);
    }
  },

  starConversation: async (id, starred) => {
    const { tenantId } = useStore.getState();
    try {
      await fetch(apiUrl(`/stakeholder/${tenantId}/conversations/${id}`), {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ starred }),
      });
      await useStore.getState().fetchConversations();
    } catch (e) {
      console.error(e);
    }
  },

  deleteConversation: async (id) => {
    const { tenantId, stakeholder } = useStore.getState();
    try {
      await fetch(apiUrl(`/stakeholder/${tenantId}/conversations/${id}`), { method: 'DELETE' });
      if (stakeholder.activeConversationId === id) {
        useStore.getState().startNewConversation();
      }
      await useStore.getState().fetchConversations();
    } catch (e) {
      console.error(e);
    }
  },

  submitFeedback: async (answerId, rating) => {
    const { tenantId } = useStore.getState();
    try {
      await fetch(apiUrl(`/stakeholder/${tenantId}/feedback`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer_id: answerId, rating }),
      });
      set((state) => ({
        stakeholder: {
          ...state.stakeholder,
          messages: state.stakeholder.messages.map((m) =>
            m.answer_id === answerId ? { ...m, feedback: rating } : m),
        },
      }));
    } catch (e) {
      console.error(e);
    }
  },

  toggleReportBuilder: () => set(state => ({
    stakeholder: { ...state.stakeholder, reportBuilderOpen: !state.stakeholder.reportBuilderOpen },
  })),
  toggleAnswerSelected: (answerId) => set(state => {
    const cur = state.stakeholder.selectedAnswerIds;
    const next = cur.includes(answerId) ? cur.filter(id => id !== answerId) : [...cur, answerId];
    return { stakeholder: { ...state.stakeholder, selectedAnswerIds: next } };
  }),
  selectAllAnswers: () => set(state => ({
    stakeholder: {
      ...state.stakeholder,
      selectedAnswerIds: state.stakeholder.messages.map(m => m.answer_id).filter(Boolean),
    },
  })),
  clearSelectedAnswers: () => set(state => ({
    stakeholder: { ...state.stakeholder, selectedAnswerIds: [] },
  })),
  exportStoryline: async (format) => {
    // Clear first: without this a stale error from the previous attempt reads as a
    // failure of the one the user just started.
    set((state) => ({ stakeholder: { ...state.stakeholder, exportError: '' } }));
    const { tenantId } = useStore.getState();
    const { activeConversationId, selectedAnswerIds } = useStore.getState().stakeholder;
    if (!activeConversationId || selectedAnswerIds.length === 0) return;
    try {
      const res = await fetch(
        apiUrl(`/stakeholder/${tenantId}/conversations/${activeConversationId}/export`),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ answer_ids: selectedAnswerIds, format }),
        });
      if (!res.ok) {
        // Every export failure the backend raises (400 unknown/empty answer_ids,
        // 404 conversation, 503 docx unavailable) must reach the user; silently
        // returning here made them indistinguishable from a no-op button.
        let detail = res.statusText;
        try {
          const body = await res.json();
          if (body && body.detail) {
            detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
          }
        } catch (parseErr) {
          console.error(parseErr);
        }
        set((state) => ({
          stakeholder: { ...state.stakeholder, exportError: detail || 'Export failed' },
        }));
        return;
      }
      const blob = await res.blob();
      const disposition = res.headers.get('content-disposition') || '';
      const match = disposition.match(/filename="([^"]+)"/);
      const filename = match ? match[1] : `storyline.${format === 'docx' ? 'docx' : 'md'}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (e) {
      console.error(e);
      set((state) => ({
        stakeholder: {
          ...state.stakeholder,
          exportError: e instanceof Error ? e.message : 'Export failed',
        },
      }));
    }
  },

  junior: { logs: [], isConnected: false },
  setJunior: (data) => set((state) => ({ junior: { ...state.junior, ...data } })),
  appendJuniorLog: (log) => set((state) => ({ junior: { ...state.junior, logs: [log, ...state.junior.logs].slice(0, 100) } })),

  pipeline: { catalog: null, loading: true, refreshing: false, hasFetched: false },
  setPipeline: (data) => set((state) => ({ pipeline: { ...state.pipeline, ...data } })),

  triage: { activeTab: 'senior', knowledgeFilterStatus: 'CANDIDATE', seniorQueue: [], triageQueue: [], loading: true, hasFetched: false },
  setTriage: (data) => set((state) => ({ triage: { ...state.triage, ...data } })),
  approveKnowledgeNodes: async (ids) => {
    const { tenantId, triage } = useStore.getState();
    try {
      await fetch(apiUrl(`/triage/${tenantId}/approve`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, by: 'admin' })
      });
      const statusParam = triage.knowledgeFilterStatus === 'ALL' ? '' : `?status=${triage.knowledgeFilterStatus}`;
      const res = await fetch(apiUrl(`/triage/${tenantId}/queue${statusParam}`));
      const data = await res.json();
      set((state) => ({ triage: { ...state.triage, triageQueue: Array.isArray(data) ? data : [] } }));
    } catch (e) {
      console.error(e);
    }
  },
  rejectKnowledgeNodes: async (ids) => {
    const { tenantId, triage } = useStore.getState();
    try {
      await fetch(apiUrl(`/triage/${tenantId}/reject`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, by: 'admin' })
      });
      const statusParam = triage.knowledgeFilterStatus === 'ALL' ? '' : `?status=${triage.knowledgeFilterStatus}`;
      const res = await fetch(apiUrl(`/triage/${tenantId}/queue${statusParam}`));
      const data = await res.json();
      set((state) => ({ triage: { ...state.triage, triageQueue: Array.isArray(data) ? data : [] } }));
    } catch (e) {
      console.error(e);
    }
  },
  reviewSeniorRun: async (runId, action) => {
    const { tenantId } = useStore.getState();
    try {
      await fetch(apiUrl(`/senior/${tenantId}/review`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: runId, action, by: 'admin' })
      });
      const res = await fetch(apiUrl(`/senior/${tenantId}/queue`));
      const data = await res.json();
      set((state) => ({ triage: { ...state.triage, seniorQueue: Array.isArray(data) ? data : [] } }));
    } catch (e) {
      console.error(e);
    }
  },

  research: { file: null, query: '', results: [], loading: false, uploadStatus: '' },
  setResearch: (data) => set((state) => ({ research: { ...state.research, ...data } })),

  kpis: {
    kpisList: [], creatableKpis: [], loading: true, expandedRow: null,
    trendingData: {}, editingSql: {}, showAddKpi: false,
    newKpi: { name: '', description: '', sql_query: '', target: 0, time_unit: 'day' },
    hasFetched: false
  },
  setKpis: (data) => set((state) => ({ kpis: { ...state.kpis, ...data } })),

  governance: { retentionDays: 30, piiDetection: true },
  setGovernance: (data) => set((state) => ({ governance: { ...state.governance, ...data } })),

  observability: { logs: [], status: {}, loading: true, purging: false, triggering: false, hasFetched: false },
  setObservability: (data) => set((state) => ({ observability: { ...state.observability, ...data } })),

  config: {
    profile: {}, targets: [], aiConfig: null, loading: true, saving: false,
    savingProfile: false, showAddTarget: false,
    newTarget: { table_name: '', strategy: 'FULL', schedule: '0 0 * * *', sql_query: '' },
    hasFetched: false
  },
  setConfig: (data) => set((state) => ({ config: { ...state.config, ...data } })),
}));
