'use client';

import { CodeBlock, Disclosure } from '@/components/analyst/Disclosure';
import type { AnalysisArtifact, ExtractMeta } from '@/types/analysis';

/**
 * The turn's provenance, one click away and closed by default.
 *
 * This is what the whole of Plan A was building toward: every field it records
 * per turn becomes inspectable here. The ordering constraint that matters is
 * that the answer stays prose and a chart, while the datasets, the SQL, the
 * Python and the methodology sit underneath it -- reachable in seconds by
 * someone challenged on a number, invisible to someone who just wants the
 * number.
 *
 * Uncertainty deliberately does NOT live here. unresolved_terms and the
 * truncation warnings render in the message body, above the fold; filing "this
 * is not a defined metric" under Methodology would defeat the entire mechanism.
 */

const RED = 'var(--error)';

function Warning({ children }: { children: React.ReactNode }) {
  return (
    <p style={{ color: RED, fontSize: '0.85rem', margin: '0.25rem 0' }}>{children}</p>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  if (value === null || value === undefined || value === '') return null;
  return (
    <div style={{ display: 'flex', gap: '0.5rem', fontSize: '0.85rem', padding: '0.1rem 0' }}>
      <span style={{ color: 'var(--text-muted)', minWidth: '9rem', flexShrink: 0 }}>{label}</span>
      <span style={{ color: 'var(--text-secondary)' }}>{value}</span>
    </div>
  );
}

function DataUsed({
  analysis, extractMeta, download,
}: {
  analysis: AnalysisArtifact;
  extractMeta?: ExtractMeta;
  download?: React.ReactNode;
}) {
  const datasets = analysis.datasets_used ?? [];
  const coverage = analysis.coverage;
  if (!datasets.length && !extractMeta?.label && !coverage) return null;

  return (
    <Disclosure label="Data used" count={datasets.length}>
      {datasets.length > 0 && <Field label="Datasets" value={datasets.join(', ')} />}
      {extractMeta?.grain?.length ? (
        <Field label="Grain" value={`one row per ${extractMeta.grain.join(', ')}`} />
      ) : null}
      {extractMeta?.row_count !== undefined && (
        <Field label="Rows" value={extractMeta.row_count.toLocaleString()} />
      )}
      {extractMeta?.columns?.length ? (
        <Field label="Columns" value={extractMeta.columns.join(', ')} />
      ) : null}
      {analysis.base_view && <Field label="Base view" value={analysis.base_view} />}
      {coverage?.decision && (
        <Field
          label="Coverage"
          // reason is written for a human by the DataManager, so it is shown
          // verbatim rather than re-worded here.
          value={coverage.reason ? `${coverage.decision} — ${coverage.reason}` : coverage.decision}
        />
      )}

      {/* Called out here, in red, rather than buried: both change how much
          weight the number deserves. */}
      {extractMeta?.truncated && (
        <Warning>
          truncated at {(extractMeta.row_count ?? 0).toLocaleString()} rows — totals
          and rates may be understated
        </Warning>
      )}
      {extractMeta?.grain_violated && (
        <Warning>
          the grain was violated — rows may be double-counted in totals
        </Warning>
      )}

      {download}
    </Disclosure>
  );
}

function Sql({ analysis }: { analysis: AnalysisArtifact }) {
  const warehouse = analysis.warehouse_sql ?? [];
  const workspace = analysis.workspace_sql ?? [];
  if (!warehouse.length && !workspace.length) return null;

  return (
    <Disclosure label="SQL" count={warehouse.length + workspace.length}>
      {/* Separately labelled, and this is a correctness matter rather than a
          cosmetic one: "this ran against Athena" and "this ran locally over
          cached Parquet" are different claims about where a number came from. */}
      {warehouse.map((sql, i) => (
        <CodeBlock key={`wh-${i}`} label="Warehouse (Athena)" code={sql} />
      ))}
      {workspace.map((sql, i) => (
        <CodeBlock key={`ws-${i}`} label="Workspace (DuckDB, local)" code={sql} />
      ))}
    </Disclosure>
  );
}

function AnalysisCode({ analysis }: { analysis: AnalysisArtifact }) {
  const cells = analysis.python_code ?? [];
  if (!cells.length) return null;

  return (
    <Disclosure label="Analysis code" count={cells.length}>
      {cells.map((code, i) => <CodeBlock key={i} code={code} />)}
      {analysis.result_summary !== undefined && analysis.result_summary !== null && (
        <CodeBlock label="Result" code={JSON.stringify(analysis.result_summary, null, 2)} />
      )}
    </Disclosure>
  );
}

function Methodology({ analysis }: { analysis: AnalysisArtifact }) {
  const rationale = analysis.plan_rationale ?? '';
  const semantics = analysis.semantics_used ?? [];
  const requirement = analysis.requirement ?? {};
  const assumptions = analysis.assumptions ?? [];
  const requirementKeys = Object.keys(requirement);

  if (!rationale && !semantics.length && !requirementKeys.length && !assumptions.length) {
    return null;
  }

  return (
    <Disclosure label="Methodology">
      {rationale && <Field label="Plan" value={rationale} />}
      {semantics.length > 0 && (
        <Field label="Definitions used" value={semantics.join(', ')} />
      )}
      {requirementKeys.length > 0 && (
        <div style={{ marginTop: '0.5rem' }}>
          {/* A small key/value table rather than raw JSON -- the requirement is
              meant to be read by the person defending the number. */}
          {requirementKeys.map((k) => (
            <Field
              key={k}
              label={k}
              value={typeof requirement[k] === 'object'
                ? JSON.stringify(requirement[k])
                : String(requirement[k])}
            />
          ))}
        </div>
      )}
      {assumptions.length > 0 && (
        <ul style={{
          margin: '0.5rem 0 0', paddingLeft: '1.2rem', fontSize: '0.85rem',
          color: 'var(--text-secondary)',
        }}>
          {/* Attribution rules arrive here already phrased as sentences by the
              backend (_attribution_caveat), so they are rendered rather than
              re-derived -- two spellings of the same rule is how a UI and a
              document start disagreeing. */}
          {assumptions.map((a, i) => <li key={i}>{a}</li>)}
        </ul>
      )}
    </Disclosure>
  );
}

export function AnalysisDisclosures({
  analysis, extractMeta, download,
}: {
  analysis?: AnalysisArtifact;
  extractMeta?: ExtractMeta;
  download?: React.ReactNode;
}) {
  // A pre-Plan-A row replayed from the database has no artifact at all, and the
  // message still has to work.
  if (!analysis) return null;

  return (
    <div>
      <DataUsed analysis={analysis} extractMeta={extractMeta} download={download} />
      <Sql analysis={analysis} />
      <AnalysisCode analysis={analysis} />
      <Methodology analysis={analysis} />
    </div>
  );
}
