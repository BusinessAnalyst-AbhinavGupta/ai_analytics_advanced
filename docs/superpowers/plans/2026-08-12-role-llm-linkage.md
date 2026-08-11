# Role-Based LLM Linkage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure that the database-backed per-tenant AI configs (`AnalystConfig`) are dynamically resolved and utilized by the Junior, Senior, and Stakeholder engines at runtime, rather than relying on a statically injected global LLM client.

**Architecture:** We will introduce a new factory method `make_role_client` in `client.py` that respects tenant overrides while hydrating global keys. The core engines will be refactored to fetch the tenant config just-in-time and use this factory, completely decoupling from the global LLM injection loop.

**Tech Stack:** Python, FastAPI, Pydantic

## Global Constraints
- Must not leak LLM API keys into the database (keys remain read from `Settings`).
- Must safely fall back to `NullClient` if the provider is `null` or unconfigured.

---

### Task 1: `make_role_client` Factory

**Files:**
- Modify: `analytics_platform/llm/client.py`
- Modify: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Settings`, `AnalystAI`
- Produces: `make_role_client(settings: Settings, role_ai: AnalystAI) -> LLMClient`

- [ ] **Step 1: Write the failing test**
```python
def test_make_role_client(self):
    s = _Settings(llm_provider="openrouter", llm_model="global-model")
    from analytics_platform.domain import AnalystAI
    role_ai = AnalystAI(role="junior", enabled=True, provider="ollama", model="llama3")
    from analytics_platform.llm.client import make_role_client, GatewayClient
    client = make_role_client(s, role_ai)
    self.assertIsInstance(client, GatewayClient)
    self.assertEqual(client.provider, "ollama")
    self.assertEqual(client.model, "llama3")
```
- [ ] **Step 2: Run test to verify it fails**
Run: `python3 -m unittest tests.test_llm -v`
Expected: FAIL with "make_role_client not defined"

- [ ] **Step 3: Write minimal implementation**
```python
# analytics_platform/llm/client.py
from ..domain import AnalystAI

def make_role_client(settings, role_ai: AnalystAI) -> LLMClient:
    provider = role_ai.provider if role_ai.provider else settings.llm_provider
    model = role_ai.model if role_ai.model else settings.llm_model
    return make_client(provider=provider, model=model, 
                       api_key=settings.effective_api_key(), 
                       ollama_base_url=settings.ollama_base_url)
```

- [ ] **Step 4: Run test to verify it passes**
Run: `python3 -m unittest tests.test_llm -v`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add analytics_platform/llm/client.py tests/test_llm.py
git commit -m "feat(llm): add make_role_client factory to support tenant overrides"
```

---

### Task 2: Dynamically Resolve LLM in `JuniorEngine`

**Files:**
- Modify: `analytics_platform/junior.py`
- Modify: `tests/test_junior.py`

**Interfaces:**
- Consumes: `make_role_client`

- [ ] **Step 1: Modify Constructor**
Remove `llm`, `llm_cache_ttl_minutes`, and `llm_daily_cap` from the `JuniorEngine.__init__` signature (since they are tenant-specific or settings-specific now). Save `settings` on `self`.

- [ ] **Step 2: Refactor `suggest_questions`**
```python
def suggest_questions(self, tenant_id: str, company: CompanyProfile, target_node: KnowledgeNode) -> Optional[QuestionGenerationOutput]:
    cfg = self.tenants.get_analyst_config(tenant_id)
    if not cfg.junior.enabled:
        return None
    llm = make_role_client(self.settings, cfg.junior)
    # Use `llm` below instead of `self.llm`
    # Replace self.llm_daily_cap with self.settings.llm_daily_cap
    # Replace self.llm_cache_ttl_seconds with self.settings.junior_llm_cache_ttl_minutes * 60
```

- [ ] **Step 3: Run existing junior tests to fix regressions**
Run: `python3 -m unittest tests.test_junior -v`
Expected: FAIL due to `__init__` signature changes. Fix all failing tests by passing `settings` instead of `llm` to the constructor.

- [ ] **Step 4: Commit**
```bash
git add analytics_platform/junior.py tests/test_junior.py
git commit -m "feat(junior): resolve LLM dynamically from tenant AnalystConfig"
```

---

### Task 3: Dynamically Resolve LLM in `SeniorReviewer`

**Files:**
- Modify: `analytics_platform/senior.py`
- Modify: `tests/test_senior.py` (or similar tests if they exist)

**Interfaces:**
- Consumes: `make_role_client`

- [ ] **Step 1: Modify Constructor**
Remove `llm` from `SeniorReviewer.__init__` and store `settings`.

- [ ] **Step 2: Refactor `run_senior_review`**
```python
def run_senior_review(self, tenant_id: str, run_doc: Dict[str, Any]) -> None:
    cfg = self.tenants.get_analyst_config(tenant_id)
    if not cfg.senior.enabled:
        return # Fall back to human review (existing logic)
    
    # ... inside the loop where LLM is needed ...
    llm = make_role_client(self.settings, cfg.senior)
```

- [ ] **Step 3: Fix tests & Verify**
Run tests touching `senior.py` and fix constructor issues.

- [ ] **Step 4: Commit**
```bash
git add analytics_platform/senior.py
git commit -m "feat(senior): resolve LLM dynamically from tenant AnalystConfig"
```

---

### Task 4: Dynamically Resolve LLM in `StakeholderService`

**Files:**
- Modify: `analytics_platform/stakeholder.py`

**Interfaces:**
- Consumes: `make_role_client`

- [ ] **Step 1: Modify Constructor**
Remove `llm` from `StakeholderService.__init__` and store `settings`.

- [ ] **Step 2: Refactor `answer_question`**
```python
def answer_question(self, tenant_id: str, question: str, user_id: str = "") -> StakeholderAnswer:
    cfg = self.tenants.get_analyst_config(tenant_id)
    if not cfg.stakeholder.enabled:
        # Return a graceful message indicating it's disabled.
        pass 
    llm = make_role_client(self.settings, cfg.stakeholder)
```

- [ ] **Step 3: Fix tests & Verify**
Run tests touching `stakeholder.py` and fix constructor issues.

- [ ] **Step 4: Commit**
```bash
git add analytics_platform/stakeholder.py
git commit -m "feat(stakeholder): resolve LLM dynamically from tenant AnalystConfig"
```

---

### Task 5: Clean up `api.py` and `junior_worker.py` Context

**Files:**
- Modify: `analytics_platform/api.py`
- Modify: `analytics_platform/junior_worker.py`

- [ ] **Step 1: Clean up `make_context` in `api.py`**
Remove `llm=make_client_from(settings)` from the `JuniorEngine`, `SeniorReviewer`, and `StakeholderService` constructor calls in `make_context`. Provide `settings=settings`.

- [ ] **Step 2: Clean up `junior_worker.py`**
Ensure `JuniorWorker` initializes `JuniorEngine` correctly without passing `llm`.

- [ ] **Step 3: Run Full Test Suite**
Run: `python3 -m unittest discover -v`
Expected: PASS. All 200+ tests should green.

- [ ] **Step 4: Commit**
```bash
git add analytics_platform/api.py analytics_platform/junior_worker.py
git commit -m "refactor(api): remove static LLM injection in favor of JIT role resolution"
```
