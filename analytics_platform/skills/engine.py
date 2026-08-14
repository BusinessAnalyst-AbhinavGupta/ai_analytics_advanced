import json
import re
from typing import Any, Dict, List, Optional
from .registry import SkillMetaData, SkillBundle

class SkillMatch:
    def __init__(self, skill_name: str, reasoning: str):
        self.skill_name = skill_name
        self.reasoning = reasoning

class SkillExecutionResult:
    def __init__(self, ok: bool, error: str = "", data_previews: List[Dict[str, Any]] = None, queries_run: List[str] = None):
        self.ok = ok
        self.error = error
        self.data_previews = data_previews or []
        self.queries_run = queries_run or []

_PARAM_TOKEN_RE = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?')


def _required_params(skill: SkillBundle) -> List[str]:
    """Every $key / ${key} token referenced across the skill's SQL templates,
    in first-seen order. This is the deterministic parameter contract the
    templates actually need -- extraction should fill these exact names
    rather than whatever key the LLM happens to invent while reading prose,
    since a near-miss name (e.g. "days" instead of "lookback_days") leaves
    the token unsubstituted and the query fails with a raw syntax error."""
    seen: List[str] = []
    for sql in skill.sql_templates.values():
        for m in _PARAM_TOKEN_RE.finditer(sql):
            key = m.group(1)
            if key not in seen:
                seen.append(key)
    return seen


class SkillEngine:
    def match(self, question: str, skills: List[SkillMetaData], llm: Any) -> Optional[SkillMatch]:
        """Uses LLM to evaluate if the question matches any registered skill."""
        if not skills:
            return None
            
        skill_descriptions = "\n".join([f"- {s.name}: {s.description}" for s in skills])
        
        sys_prompt = (
            "You are a routing agent for an analytics platform. "
            "Your job is to read the user's question and determine if one of the specialized skills "
            "should be used to answer it. If none fit well, return null for skill_name.\n\n"
            "You MUST respond with a strict JSON object with this schema: \n"
            '{"skill_name": "the_name_of_the_skill_or_null", "reasoning": "brief explanation"}'
        )
        
        prompt = f"Available Skills:\n{skill_descriptions}\n\nUser Question: {question}\n\nDetermine the best skill to use."
        
        try:
            res = llm.generate(prompt=prompt, system_prompt=sys_prompt, temperature=0.0)
            text = (res.text or "").strip() if res and hasattr(res, "text") else ""
            
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].strip()
                
            parsed = json.loads(text)
            skill_name = parsed.get("skill_name")
            if skill_name and skill_name != "null":
                return SkillMatch(skill_name=skill_name, reasoning=parsed.get("reasoning", ""))
        except Exception:
            pass
            
        return None

    def extract_params(self, question: str, skill: SkillBundle, llm: Any) -> tuple[Dict[str, Any], bool, str]:
        """Extracts parameters for the skill, or flags if clarification is needed.
        Returns: (params_dict, needs_clarification, clarification_question)
        """
        required = _required_params(skill)

        sys_prompt = (
            "You are a parameter extraction agent. Based on the user's question and the skill description, "
            "extract the necessary parameters to execute the skill. If critical parameters are completely missing "
            "and cannot be reasonably defaulted, set 'needs_clarification' to true and provide a 'clarification_question'.\n"
            + (f"The SQL templates require a value for EXACTLY these parameter keys -- use these "
               f"names verbatim in your \"params\" object, do not invent or rename them: "
               f"{', '.join(required)}.\n" if required else "")
            + "You MUST respond with a strict JSON object with this schema: \n"
            '{"params": {"key": "value"}, "needs_clarification": false, "clarification_question": ""}'
        )

        prompt = (
            f"Skill Name: {skill.meta.name}\n"
            f"Skill Description: {skill.meta.description}\n"
            f"Skill Instructions: {skill.instructions}\n\n"
            f"User Question: {question}\n\n"
            "Extract parameters for the SQL templates or identify missing ones."
        )

        try:
            res = llm.generate(prompt=prompt, system_prompt=sys_prompt, temperature=0.0)
            text = (res.text or "").strip() if res and hasattr(res, "text") else ""

            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].strip()

            parsed = json.loads(text)
            params = parsed.get("params", {})
            needs_clarif = parsed.get("needs_clarification", False)
            clarif_q = parsed.get("clarification_question", "")

            missing = [k for k in required if k not in params or params[k] in (None, "")]
            if missing and not needs_clarif:
                # The extraction contract named these keys explicitly, but the
                # LLM still left one unfilled -- letting engine.execute() run
                # would leave a literal unsubstituted $token in the SQL and
                # fail with an opaque syntax error deep in the warehouse.
                # Degrade to a clarification request instead, same as when
                # the LLM flags this itself.
                needs_clarif = True
                clarif_q = (f"Missing required parameter(s) for skill '{skill.meta.name}': "
                            f"{', '.join(missing)}. Could you clarify?")

            return (params, needs_clarif, clarif_q)
        except Exception:
            return ({}, False, "")

    def execute(self, skill: SkillBundle, params: Dict[str, Any], executor: Any, ec: Any) -> SkillExecutionResult:
        """Executes the skill's SQL templates with the extracted parameters."""
        queries_run = []
        data_previews = []
        
        # We need a deterministic order if multiple SQL templates exist.
        # Simple heuristic: alphabetical order of filenames.
        sorted_templates = sorted(skill.sql_templates.items())
        
        for file_name, sql_template in sorted_templates:
            sql = sql_template
            for key, value in params.items():
                val_str = str(value)
                # Replace both $key and ${key}
                sql = re.sub(r'\$' + re.escape(key) + r'(?!\w)', val_str, sql)
                sql = sql.replace(f"${{{key}}}", val_str)
                
            queries_run.append(sql)
            
            result = executor.execute(sql, ec)
            if not result.ok:
                return SkillExecutionResult(ok=False, error=f"Error in {file_name}: {result.error}", queries_run=queries_run)
                
            # Collect preview
            preview = []
            rows = result.data
            if rows is not None:
                try:
                    preview = rows.head(5).to_dict(orient="records")
                except Exception:
                    preview = list(rows)[:5]
            data_previews.append({
                "step": file_name,
                "row_count": result.row_count,
                "preview": preview
            })
            
        return SkillExecutionResult(ok=True, data_previews=data_previews, queries_run=queries_run)
