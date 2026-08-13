---
name: funnel-conversion-analysis
description: Use when analyzing customer conversion funnels, building happy flows, or identifying step-by-step drop-off rates between natural language start and end points in clickstream data
---

# Funnel Conversion Analysis Skill

## Overview

This skill provides an interactive workflow for AI agents to assist analysts and business stakeholders in performing end-to-end clickstream funnel conversion analysis. It extracts natural language start and end points, calculates the representative "happy flow" using iterative hierarchical relative median ordering (`page` -> `action` -> `label`), and constructs custom SQL queries to analyze conversion drop-offs.

## When to Use

Activate this skill when:
- An analyst or stakeholder asks to analyze a user journey or conversion funnel from a natural language starting point to an ending point.
- You need to determine the canonical customer journey ("happy flow") across clickstream events.
- You need to build multi-step conversion funnel SQL queries (Presto/Trino dialect).

Do NOT use when:
- The user is asking for single ad-hoc aggregations without a sequence/funnel context.
- The data source is not clickstream session event data.

## Autonomous Agent Execution & Self-Verification Architecture

> ⚠️ **Zero-Friction Principles for Stakeholders**: 
> The stakeholder/analyst should **NEVER** be expected to manually copy-paste SQL, run queries in external DB clients, or orchestrate multi-step prompts. 
> The **AI Agent autonomously executes the SQL queries**, inspects results, self-verifies statistical sanity, and presents clean business insights.

```
Stakeholder Prompt ("Analyze funnel from Landing to Purchase")
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│ AUTOMATED AGENT LOOP (Behind the Scenes)                    │
│                                                             │
│ 1. Parse Start/End & Segment Parameters                     │
│ 2. Execute `canonical_happy_flow.sql` via Database Tool     │
│ 3. Self-Critique Path: Verify sequence, filter noise       │
│ 4. Execute `funnel_conversion_template.sql`                 │
│ 5. Self-Verify Sanity: Confirm conversion % bounds [0-100%] │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
  Deliver Final Executive Narrative & One-Click Refinement Options
```

---

### Agent Responsibilities vs. Stakeholder Experience

| Action | Handled By | How It Works |
| :--- | :--- | :--- |
| **NL Intent & Start/End Points** | Stakeholder | Types a simple natural language prompt. |
| **SQL Query Generation & Dialect Syntax** | AI Agent | Autonomously populates Presto/Trino SQL templates. |
| **SQL Engine Execution** | AI Agent | Runs queries directly via database tools or CLI without user intervention. |
| **Intermediate Path Verification** | AI Agent | Inspects SQL output, checks row counts, validates hierarchical ordering (`page` -> `action` -> `label`). |
| **Funnel Conversion Execution** | AI Agent | Constructs and runs the sequential step-by-step conversion query. |
| **Sanity & Edge-Case Audit** | AI Agent | Checks that conversion % is bounded [0%, 100%], flag anomalies. |
| **Final Insights & Decision Presentation** | AI Agent | Presents executive summary + 2-3 one-click drill-down suggestions (e.g. *"Drill down by device type"*). |

---

### Detailed Agent Execution Protocol

#### Phase 1: Parameter Extraction & Disambiguation
1. Extract Start Node (`page`, `action`, `label`) and End Node (`page`, `action`, `label`) from user prompt.
2. If optional parameters (`service_line`, `category`, `natco`) are omitted, default to standard defaults (`natco='de'`, last 15 days) and notify the user inline.

#### Phase 2: Happy Flow Discovery & Autonomous Self-Audit
1. Execute [`canonical_happy_flow.sql`](references/canonical_happy_flow.sql) via database tools.
2. **Agent Self-Audit**:
   - Verify non-empty query output.
   - Confirm relative median ranks (`page_seq`, `action_seq`, `label_seq`) are strictly monotonic.
   - Extract the ordered canonical path between Start Node and End Node.

#### Phase 3: Funnel Query Execution & Sanity Check
1. Dynamically populate [`funnel_conversion_template.sql`](references/funnel_conversion_template.sql) with the audited canonical path.
2. Execute the funnel query via database tools.
3. **Sanity Check**:
   - Check `step_N_sessions <= step_N-1_sessions` (funnel volume must be non-increasing).
   - Ensure step conversion rates are valid numbers.

#### Phase 4: Stitched Narrative Delivery
Present the complete analysis to the stakeholder in clean executive formatting:
- **Funnel Table**: Step-by-step session counts, step conversion %, overall conversion %, drop-off %.
- **Bottleneck Spotlight**: Highlight the single step with the maximum drop-off rate.
- **Proactive Follow-ups**: Offer 2-3 simple follow-up options for the stakeholder to choose (e.g. *"Option 1: Segment by service_line", "Option 2: Exclude non-retained labels"*).

## Reference Files

- [`canonical_happy_flow.sql`](references/canonical_happy_flow.sql): Presto/Trino SQL query deriving canonical happy flows with relative hierarchical median ordering down to label level.
- [`funnel_conversion_template.sql`](references/funnel_conversion_template.sql): Template SQL for calculating step-by-step conversion rates and drop-offs.

