# Dynamic Skill Engine Design Specification for `StakeholderService`

## Overview
This design specification defines the architecture for extending `StakeholderService` (`analytics_platform/stakeholder.py`) with a dynamic, decoupled **Skill Engine**. 

Instead of hardcoding single analytics workflows (such as Funnel Conversion Analysis), `StakeholderService` will dynamically discover available skills from workspace skill directories (e.g. `.agents/skills/`), evaluate whether a user's question requires a specialized skill, prompt for missing parameters when necessary, execute the skill's working SQL queries, and deliver an integrated analysis report with citations and executed queries.

---

## Key Requirements

1. **Decoupled & Extensible Architecture**: Adding new analytics skills (funnel analysis, cohort retention, churn prediction, market basket analysis) must require zero modification to the core `StakeholderService` logic.
2. **4-Tier Question Routing**:
   - **Tier 1 (High Risk / Escalation)**: PII, credentials, sensitive revenue questions -> Escalate to senior analyst review.
   - **Tier 2 (Approved Knowledge)**: Exact match in `CompanyBrain` -> Refresh and re-execute approved query/definition.
   - **Tier 3 (Specialized Analytics Skill)**: Matches a registered skill in `.agents/skills/` -> Execute skill pipeline, extract parameters, run multi-step SQL templates.
   - **Tier 4 (Direct LLM Synthesis / No Skill Required)**: Simple definitions, ad-hoc text explanations -> Answer directly using low-cost LLM without skill overhead.
3. **Execution Transparency**: For Tier 3 skill responses, the system MUST return:
   - The executed, working SQL queries (`queries_run`).
   - Step-by-step data analysis derived from query execution.
   - Interactive chart configurations (`chart_config`).
   - Follow-up options / recommendations.
4. **Parameter Disambiguation & Follow-up**: If a matched skill requires missing parameters (e.g. starting/ending points for funnel analysis), the system prompts the user or defaults intelligently.

---

## System Architecture & Components

```
                   User Natural Language Question
                                 │
                                 ▼
                     StakeholderService.answer()
                                 │
           ┌─────────────────────┴─────────────────────┐
           ▼                                           ▼
   High-Risk Check                            CompanyBrain Search
(PII / Secret / Revenue)                     (Approved Queries/Defs)
           │                                           │
       [Escalate]                                  [Re-run Query]
           │                                           │
           └─────────────────────┬─────────────────────┘
                                 │ (No Match)
                                 ▼
                       SkillEngine.evaluate()
                                 │
                 ┌───────────────┴───────────────┐
                 ▼                               ▼
       Skill Match Found (Tier 3)      No Skill Needed (Tier 4)
                 │                               │
       SkillExecutor.run()               Low-Cost LLM Synthesis
  (Load SQL -> Exec -> Synthesize)               │
                 │                               │
                 └───────────────┬───────────────┘
                                 ▼
                    Final Formatted Response
            (Answer + Queries Run + Charts + Cost)
```

---

## Detailed Component Specifications

### 1. `SkillRegistry` (`analytics_platform/skills/registry.py`)
Responsible for scanning and registering available skills dynamically.

- **Discovery Location**: `.agents/skills/` within the active workspace.
- **Skill Structure**:
  - `SKILL.md`: Frontmatter (`name`, `description` with triggering conditions).
  - `references/*.sql`: Parameterized SQL templates used by the skill.
- **Methods**:
  - `load_skills() -> List[SkillMetaData]`: Scans `.agents/skills/` and parses YAML frontmatter.
  - `get_skill(name: str) -> Optional[SkillBundle]`: Returns `SKILL.md` content and attached SQL reference files.

### 2. `SkillEngine` (`analytics_platform/skills/engine.py`)
Responsible for matching questions to skills and executing multi-step skill pipelines.

- **Methods**:
  - `match(question: str, skills: List[SkillMetaData], llm: Any) -> Optional[SkillMatch]`: Uses low-cost LLM prompt matching to evaluate if the question matches any registered skill description. Returns `None` if no skill is needed (Tier 4).
  - `extract_params(question: str, skill: SkillBundle, llm: Any) -> Dict[str, Any]`: Extracts parameters (e.g. start/end page/action/label) required by the skill.
  - `execute(skill: SkillBundle, params: Dict[str, Any], executor: Any, ec: ExecutionContext) -> SkillExecutionResult`:
    1. Reads parameterized SQL templates from `references/`.
    2. Substitutes extracted parameters.
    3. Runs queries sequentially using `executor.execute(sql, ec)`.
    4. Evaluates query row results, checks for errors, and aggregates data previews.

### 3. `StakeholderService` Updates (`analytics_platform/stakeholder.py`)
Enhance `StakeholderService.answer()` to integrate `SkillEngine`:

```python
# Updated Answer Mode Enum
class AnswerMode(Enum):
    DIRECT_FROM_APPROVED_KNOWLEDGE = "DIRECT_FROM_APPROVED_KNOWLEDGE"
    REFRESHED_APPROVED_QUERY = "REFRESHED_APPROVED_QUERY"
    SKILL_EXECUTED_ANALYSIS = "SKILL_EXECUTED_ANALYSIS"      # <--- NEW
    NEW_LOW_RISK_ANALYSIS = "NEW_LOW_RISK_ANALYSIS"
    REQUIRES_SENIOR_REVIEW = "REQUIRES_SENIOR_REVIEW"
    CANNOT_ANSWER = "CANNOT_ANSWER"
```

#### Updated Routing Logic Flow:
1. **Check Disabled / High-Risk**: Escalate if PII/sensitive terms matched.
2. **Check CompanyBrain**: Re-run approved query if present.
3. **Check Skill Engine**:
   - Call `SkillEngine.match(question)`.
   - If matched: Call `SkillEngine.execute()`, record executed SQL queries (`queries_run`), synthesize response using LLM + chart configs, and return `AnswerMode.SKILL_EXECUTED_ANALYSIS`.
4. **Fallback to Direct LLM Synthesis**: If no skill matched, execute direct low-cost LLM synthesis without skill overhead (`AnswerMode.NEW_LOW_RISK_ANALYSIS`).

---

## Data Output Schema

The output dictionary returned by `StakeholderService.answer()` for skill-executed answers will be structured as:

```json
{
  "answer_id": "ans_12345",
  "tenant_id": "tenant_abc",
  "question": "Show conversion funnel from homepage to order checkout",
  "category": "process",
  "answer_mode": "SKILL_EXECUTED_ANALYSIS",
  "answer": "Conversion analysis complete. Step 1 (homepage -> product_list) converted at 65.0%, while Step 2 (product_list -> checkout) experienced the highest drop-off at 67.7%.",
  "queries_run": [
    "-- Canonical Happy Flow Query\nWITH base AS (...)...",
    "-- Funnel Conversion Calculation Query\nWITH base_events AS (...)..."
  ],
  "chart_config": {
    "type": "BarChart",
    "xKey": "step_name",
    "series": [{"key": "conversion_pct"}]
  },
  "chart_data": [
    {"step_name": "Step 1: Homepage", "conversion_pct": 100.0},
    {"step_name": "Step 2: Product List", "conversion_pct": 65.0},
    {"step_name": "Step 3: Checkout", "conversion_pct": 21.0}
  ],
  "cost": 0.0012,
  "status": "ANSWERED"
}
```

---

## Verification & Testing Plan

1. **Unit Tests (`tests/test_skill_engine.py`)**:
   - Test `SkillRegistry` discovery of `.agents/skills/funnel-conversion-analysis`.
   - Test `SkillEngine.match()` matching funnel questions correctly to `funnel-conversion-analysis`.
   - Test `SkillEngine.match()` returning `None` for generic questions (e.g. "What is active user definition?").
   - Test `StakeholderService.answer()` returning `queries_run` array containing valid SQL strings.

2. **Integration Verification**:
   - Run `pytest tests/` to ensure no regression on existing `CompanyBrain` or high-risk escalation paths.
