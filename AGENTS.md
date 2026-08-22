# Agent Guidelines & Framework
> This is the "Holy Grail" for AI agents working in this repository. All instructions below are mandatory and must be strictly followed.

## PART 1: Rules for AI Coding Agents Building This Tool
*These rules apply to any AI coding assistant (like Gemini) modifying this repository.*

### 1. Engineering & Architectural Standards
- **Production-Grade Delivery:** The tool is built for production, not as a hobby project. Code must be robust, typed, scalable, and handle errors gracefully.
- **Core vs. Tenant Development:** When adding a new feature, it MUST be built into the `core` platform logic, never hardcoded for a specific tenant. 
- **API-First Interactions:**
  - The UI must be a thin client. All interactions between the frontend (e.g., Streamlit) and the backend must occur via API boundaries.
  - Internal backend services and components should also interact via strict API/interface contracts to support observability and modularity.
- **LLM Configurability:** Every LLM utilized in the platform must have its configuration (provider, model, parameters) exposed and available in the tool's configuration system.
- **No Silent Hardcoding:** If a value must be hardcoded for temporary or structural reasons, it must be documented immediately in a `HARDCODED_REGISTRY.md` file in the project root. Detail **What** is hardcoded, **Where** it is located, and **Why** it was necessary.

### 2. Multi-Tenancy & Version Control
- **The Object-Oriented Mental Model:** The codebase in Git represents the *Class* (the core tool capable of onboarding any company). The tenants are the *Objects* (instances of the tool).
- **Strict Isolation:** Data, schemas, queries, and LLM API keys must always be strictly isolated and treated as tenant-specific. 
- **Git Ignored Tenants:** Anything specific to a tenant (databases, local config files, credentials) must be excluded via `.gitignore`. The repository should only contain the universal, tenant-agnostic application.
- **Continuous Commits:** Agents should commit to Git at every major checkpoint. This includes, but is not limited to, the completion of a new feature, refactoring, or a critical bug fix.

### 3. Operational Governance & Safety
- **Human-in-the-Loop:** Agents generate hypotheses, execute SQL, and profile data, but humans approve knowledge into the Company Brain. Nothing generated silently becomes company fact without review.
- **Read-Only Database Access:** Agents are strictly limited to `SELECT`, metadata inspection, and `EXPLAIN` queries when interacting with customer data.
- **Dimensional Confidence:** Confidence in a finding is not a single weight. Agents must evaluate findings across multiple dimensions: Evidence, Review status, Freshness, and Data Quality.

### 4. Communicating with the Repository Owner
- **Plain English, Always:** Every message written for the owner to read — progress updates, findings, anything requiring approval or a decision — must be in simple, non-technical English. The owner does not read the code and must never be required to in order to understand an update or answer a question.
- **No Code in Explanations:** Do not put file paths, line numbers, function or variable names, code snippets, or library names in anything written for the owner. Describe what a thing *does* in everyday words instead.
- **No Jargon:** Terms like "wrapper", "endpoint", "schema", "context variable", or "generator" are not explanations. Replace them with what the reader would actually observe.
- **Say the Three Things:** What changed, whether it works, and what decision is needed. Outcomes such as "all checks pass" or "36 new checks added" are fine — those are results, not code.
- **Be Short:** A few plain sentences beat a formatted table of technical terms.
- **Scope:** This governs conversation and anything asking for a decision. Commit messages, code comments, specs, and plans remain fully technical — they are written for engineers, not for the owner.

Technical precision still governs the work itself. It simply does not belong in what the owner reads.

---

## PART 2: Rules for the Stakeholder Analyst (Application AI)
*When building or modifying the Stakeholder Analyst persona within the platform, ensure these frameworks are embedded in its logic/prompts.*

### 1. Diagnostic Framework for Funnels
When conducting funnel or canonical journey analysis, the Stakeholder Analyst must categorize identified drop-offs or bottlenecks into one of the four friction types:
- **Matching Friction:** Were the wrong users acquired for the product?
- **Educational Friction:** Do users not understand the value proposition or the next step?
- **Operational Friction:** Is there a broken UI, a bug, performance issues, or UX flow problems?
- **Motivational Friction:** Do users lack the incentive or psychological push to proceed?

### 2. Metrics Hierarchy & Guardrails
When analyzing or defining metrics, the Stakeholder Analyst must categorize them into the Metrics Tree to enforce a structured relationship to the business's goals:
- **North Star Metric (NSM):** The single outcome representing value (e.g., total listening minutes per active user per month).
- **Input Metrics:** Components that mathematically contribute to the NSM (e.g., active days/month × sessions/day × minutes/session).
- **Driver Metrics:** Operational levers owned by teams that move the input metrics (e.g., push-open rate, click rate, return rate).
- **Guardrails:** Metrics that protect against harmful optimization (e.g., trust, technical health, economics, long-term retention).
- **Rule:** Any optimization or prescriptive action targeting a Driver Metric MUST explicitly state and check a relevant Guardrail metric.

---

## PART 3: Rules for the Junior Analyst (Application AI)
*When building or modifying the Junior Analyst persona within the platform, ensure this sequence is embedded in its workflow.*

### 1. The Analytics Sequence
The Junior Analyst must act as a strategic analyst whose work changes decisions, not just a reporter of data. **Sequence matters.**
- **Descriptive Layer (What is happening?):** Always establish the hard facts first (e.g., "10,000 signups; checkout completion = 62%"). 
- **Diagnostic Layer (Why is it happening?):** Form hypotheses about the drivers of the descriptive data (e.g., "Hypotheses about the drivers of low conversion").
- **Prescriptive Layer (What should we do?):** Output prioritized recommendations and actions. 
- **Rule:** Never jump directly to prescriptive recommendations without explicitly establishing the descriptive facts and diagnosing plausible causes.

### 4. Querying Standards & Schema Discovery
- **Use LIMIT 1 for Schema Probing:** When executing test queries or probing tables to determine their schema (e.g., in `JuniorEngine` or during catalog generation), always use `LIMIT 1` instead of `LIMIT 0`. Some backend execution engines or databases (like Athena via Metabase) may reject `LIMIT 0` as invalid, leading to artificial cataloging failures.
