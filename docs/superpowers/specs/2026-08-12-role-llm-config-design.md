# Role-Based LLM Configuration & Linkages

## Objective
The platform currently stores role-specific LLM configurations (`junior`, `senior`, `stakeholder`), `junior_depth` (maturity level), and `human_signoff_days` (learning period) in the database via the `AnalystConfig` entity. However, the application uses a single globally injected LLM client. This design outlines how we will refactor the platform to dynamically resolve LLM clients just-in-time, ensuring these configurations have actual linkages in the execution pathways.

## Proposed Changes

### 1. Dynamic LLM Factory in `llm/client.py`
We will introduce a new factory method:
```python
def make_role_client(settings: Settings, role_ai: AnalystAI) -> LLMClient:
    """Builds an LLM client using the role-specific config if provided,
    otherwise falls back to the global default in Settings."""
```
This ensures API keys are securely hydrated from the environment while allowing the specific `provider` and `model` to come from the database config.

### 2. Refactoring `JuniorEngine` (analytics_platform/junior.py)
- Remove the static `self.llm` injected via the constructor.
- Introduce an internal helper `_get_llm(tenant_id)` that fetches `self.tenants.get_analyst_config(tenant_id).junior`.
- Check if `junior.enabled == False`. If so, abort processing (e.g., in `suggest_questions`).
- Otherwise, resolve the client via `make_role_client` and execute the LLM hook.
- Ensure `junior_depth` continues to accurately gate maturity phases (0-3) as it does currently.

### 3. Refactoring `SeniorReviewer` (analytics_platform/senior.py)
- Remove the static `self.llm` injected via the constructor.
- Introduce `_get_llm(tenant_id)` mapped to `AnalystConfig.senior`.
- The Learning Period (`human_signoff_days`) is already partially implemented conceptually but we will verify its linkage. If the tenant age or node age is within `human_signoff_days`, the Senior AI should either skip auto-approval or explicitly flag it for human review.

### 4. Refactoring `StakeholderService` (analytics_platform/stakeholder.py)
- Remove the static `self.llm` injected via the constructor.
- Introduce `_get_llm(tenant_id)` mapped to `AnalystConfig.stakeholder`.
- If `stakeholder.enabled == False`, return a structured response indicating the AI Stakeholder feature is disabled for this tenant.

### 5. Cleaning up `api.py` Context
- The `make_context()` function will no longer instantiate a global `GatewayClient` and inject it into the engines. Instead, it relies on the engines pulling it natively just-in-time. (We may still leave the `llm` in `AppContext` for generic system-level LLM calls, but the specific role engines will ignore it).

## Verification Plan
- **Unit Tests:** Add tests mocking `get_analyst_config` to return different models for `junior` and `senior`, and assert that the correct model identifier is passed downstream to the LLM Gateway.
- **End-to-End Test:** Start the server, modify the config panel in the UI to use a different model for the junior, and trace the generated API request payload to verify the correct model was used.
