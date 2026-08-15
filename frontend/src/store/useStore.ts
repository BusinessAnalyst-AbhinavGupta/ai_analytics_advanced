import { create } from 'zustand';

export type ConversationSummary = {
  id: string; title: string; starred: boolean;
  created_at: string; updated_at: string; message_count: number;
};

export type StakeholderMessage = {
  answer_id: string; question: string; answer: string; answer_mode: string;
  status: string; citations: any[]; caveats: string[]; facts: string[];
  queries_run: string[]; escalated: boolean; cost: number; created_at: string;
  chart_config?: any; chart_data?: any[]; feedback?: 'up' | 'down';
  python_cells?: Array<{ code: string; df_label: string; result_summary: unknown }>;
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
  },
  setStakeholder: (data) => set((state) => ({ stakeholder: { ...state.stakeholder, ...data } })),

  fetchConversations: async () => {
    const { tenantId } = useStore.getState();
    if (!tenantId) return;
    set((state) => ({ stakeholder: { ...state.stakeholder, conversationsLoading: true } }));
    try {
      const res = await fetch(`http://localhost:8000/stakeholder/${tenantId}/conversations`);
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
      const res = await fetch(`http://localhost:8000/stakeholder/${tenantId}/conversations/${id}`);
      if (!res.ok) return;
      const data = await res.json();
      set((state) => ({
        stakeholder: { ...state.stakeholder, activeConversationId: id, messages: data.messages || [] },
      }));
    } catch (e) {
      console.error(e);
    }
  },

  startNewConversation: () => {
    set((state) => ({ stakeholder: { ...state.stakeholder, activeConversationId: '', messages: [], question: '' } }));
  },

  askStakeholder: async (text) => {
    const { tenantId, stakeholder } = useStore.getState();
    const queryText = text || stakeholder.question;
    if (!queryText || !tenantId) return;
    set((state) => ({ stakeholder: { ...state.stakeholder, loading: true } }));
    try {
      const res = await fetch(`http://localhost:8000/stakeholder/${tenantId}/answer`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: queryText, conversation_id: stakeholder.activeConversationId }),
      });
      const data = await res.json();
      set((state) => ({
        stakeholder: {
          ...state.stakeholder,
          question: '',
          activeConversationId: data.conversation_id || state.stakeholder.activeConversationId,
          messages: [...state.stakeholder.messages, data],
        },
      }));
      await useStore.getState().fetchConversations();
    } catch (e) {
      console.error(e);
    }
    set((state) => ({ stakeholder: { ...state.stakeholder, loading: false } }));
  },

  renameConversation: async (id, title) => {
    const { tenantId } = useStore.getState();
    try {
      await fetch(`http://localhost:8000/stakeholder/${tenantId}/conversations/${id}`, {
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
      await fetch(`http://localhost:8000/stakeholder/${tenantId}/conversations/${id}`, {
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
      await fetch(`http://localhost:8000/stakeholder/${tenantId}/conversations/${id}`, { method: 'DELETE' });
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
      await fetch(`http://localhost:8000/stakeholder/${tenantId}/feedback`, {
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
      await fetch(`http://localhost:8000/triage/${tenantId}/approve`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, by: 'admin' })
      });
      const statusParam = triage.knowledgeFilterStatus === 'ALL' ? '' : `?status=${triage.knowledgeFilterStatus}`;
      const res = await fetch(`http://localhost:8000/triage/${tenantId}/queue${statusParam}`);
      const data = await res.json();
      set((state) => ({ triage: { ...state.triage, triageQueue: Array.isArray(data) ? data : [] } }));
    } catch (e) {
      console.error(e);
    }
  },
  rejectKnowledgeNodes: async (ids) => {
    const { tenantId, triage } = useStore.getState();
    try {
      await fetch(`http://localhost:8000/triage/${tenantId}/reject`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, by: 'admin' })
      });
      const statusParam = triage.knowledgeFilterStatus === 'ALL' ? '' : `?status=${triage.knowledgeFilterStatus}`;
      const res = await fetch(`http://localhost:8000/triage/${tenantId}/queue${statusParam}`);
      const data = await res.json();
      set((state) => ({ triage: { ...state.triage, triageQueue: Array.isArray(data) ? data : [] } }));
    } catch (e) {
      console.error(e);
    }
  },
  reviewSeniorRun: async (runId, action) => {
    const { tenantId } = useStore.getState();
    try {
      await fetch(`http://localhost:8000/senior/${tenantId}/review`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_id: runId, action, by: 'admin' })
      });
      const res = await fetch(`http://localhost:8000/senior/${tenantId}/queue`);
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
