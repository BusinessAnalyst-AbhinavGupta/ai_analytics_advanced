import { create } from 'zustand';

interface AppState {
  // Global
  tenantId: string;
  setTenantId: (id: string) => void;

  // Stakeholder Q&A
  stakeholder: {
    question: string;
    answer: any;
    loading: boolean;
  };
  setStakeholder: (data: Partial<AppState['stakeholder']>) => void;

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
  tenantId: '1',
  setTenantId: (id) => set({ tenantId: id }),

  stakeholder: { question: '', answer: null, loading: false },
  setStakeholder: (data) => set((state) => ({ stakeholder: { ...state.stakeholder, ...data } })),

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
