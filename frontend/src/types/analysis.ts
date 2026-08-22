// TypeScript mirrors of Plan A's provenance dataclasses. This is the single
// source of truth for the analysis payload shape: typed once here means a
// backend field rename surfaces as a compile error rather than as `undefined`
// in a stakeholder's face.
//
// Almost everything is optional on purpose. `get_conversation` replays rows
// written before Plan A shipped, so a historical turn legitimately has no
// `analysis` at all and every component has to survive that.

export type AttributionRule = {
  column: string;
  grain?: string[];
  strategy?: string;
  priority_values?: string[];
  tiebreakers?: string[];
  source?: string;
};

export type ChartSpec = {
  kind: string;                 // bar | line | area | scatter (case-insensitive)
  x: string;
  y: string | string[];
  series?: string;              // the GROUPING column, not a recharts series
  title?: string;
};

export type CoverageVerdict = {
  decision: 'reuse' | 'extend' | 'widen' | 'retrieve';
  label?: string;
  missing_dimensions?: string[];
  missing_measures?: string[];
  missing_time_ranges?: [string, string][];
  supersedes?: string;
  existing_dimensions?: string[];
  reason?: string;              // written for a human; shown verbatim
};

export type ExtractMeta = {
  label: string;
  description?: string;
  grain?: string[];
  columns?: string[];
  dtypes?: Record<string, string>;
  row_count?: number;
  truncated?: boolean;          // hit the ceiling; totals may be understated
  sql?: string;
  created_at?: string;
  base_view?: string;
  population_hash?: string;
  dimensions?: string[];
  attributions?: AttributionRule[];
  grain_violated?: boolean;
};

export type AnalysisArtifact = {
  question?: string;
  plan_rationale?: string;
  base_view?: string;
  population_hash?: string;
  projection_hash?: string;
  base_view_approved?: boolean;
  base_view_grain_verified?: boolean;
  reconcilable?: boolean;
  slice_filters?: Record<string, string[]>;
  dimensions?: string[];
  non_additive?: string[];
  supersedes?: string;
  semantics_used?: string[];
  unresolved_terms?: string[];
  requirement?: Record<string, unknown>;
  coverage?: CoverageVerdict;
  datasets_used?: string[];
  warehouse_sql?: string[];     // ran against Athena
  workspace_sql?: string[];     // ran locally, over cached Parquet
  python_code?: string[];
  result_summary?: unknown;
  chart_spec?: ChartSpec | null;
  key_findings?: string[];
  assumptions?: string[];
  created_at?: string;
};

// Mirrors analytics_platform.domain.PIPELINE_STEPS. The UI renders them in this
// order and greys out the ones a turn skipped.
export const PIPELINE_STEPS = [
  'recalling', 'understanding', 'planning', 'checking_workspace',
  'retrieving', 'analysing', 'interpreting',
] as const;

export type PipelineStep = (typeof PIPELINE_STEPS)[number];

// One copy, consumed by both the live trail and the behind-the-scenes panel.
// Two copies is how `recalling` came to be emitted by the backend and rendered
// nowhere.
export const STEP_LABELS: Record<string, string> = {
  recalling: 'Recalling what we know',
  understanding: 'Understanding the question',
  planning: 'Planning the turn',
  checking_workspace: 'Checking the workspace',
  retrieving: 'Retrieving',
  analysing: 'Analysing',
  interpreting: 'Interpreting',
};

export type StepEvent = {
  step: PipelineStep;
  state: 'start' | 'done' | 'skipped' | 'abandoned';
  label: string;
  detail?: string;
  elapsed_ms?: number;
};

/**
 * One turn as the API returns it. Lives here rather than in the store because
 * `streamAnswer` produces these and the store consumes them -- declaring it in
 * the store would make the two modules import each other.
 *
 * `chart_config` and `chart_data` stay `any`: they are the pre-Plan-A chart
 * path, kept so historical turns keep their charts (see MessageChart, which
 * discriminates on shape). The artifact below is strictly typed, which is where
 * it matters -- that is the pattern not to extend.
 */
export type StakeholderMessage = {
  answer_id: string; question: string; answer: string; answer_mode: string;
  status: string; citations: unknown[]; caveats: string[]; facts: string[];
  queries_run: string[]; escalated: boolean; cost: number; created_at: string;
  conversation_id?: string;
  /* eslint-disable-next-line @typescript-eslint/no-explicit-any */
  chart_config?: any; chart_data?: any[]; feedback?: 'up' | 'down';
  python_cells?: Array<{ code: string; df_label: string; result_summary: unknown }>;
  produced_df_label?: string;
  analysis?: AnalysisArtifact;
  extract_meta?: ExtractMeta;
};

// One recorded moment inside a turn: an LLM call or a brain search. `payload`
// is deliberately loose -- the two kinds carry different keys, and the panel
// reads them defensively rather than the type pretending otherwise.
export type TraceRecord = {
  seq: number;
  ts: string;
  stage: string;
  kind: 'llm' | 'retrieval' | string;
  payload: Record<string, unknown>;
  duration_ms: number;
  tokens_in: number;
  tokens_out: number;
  ok: boolean;
};

export type AnswerTrace = {
  answer_id: string;
  trace_id: string;
  records: TraceRecord[];
};
