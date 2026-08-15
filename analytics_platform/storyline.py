"""Pure, DB-free assembly of a selective export ("storyline") from an already-fetched
stakeholder conversation dict. No I/O here -- Task 3/4's renderers and the API layer
own fetching and formatting; this module only decides WHAT goes into the export and
resolves the Code Appendix's cross-turn dependencies.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List

CHARS_PER_TOKEN_ESTIMATE = 4
WARN_TOKEN_THRESHOLD = 50_000


@dataclass
class StorylineTurn:
    answer_id: str
    question: str
    answer: str
    facts: List[str]
    caveats: List[str]
    created_at: str


@dataclass
class CodeAppendixEntry:
    label: str          # df_label this code relates to, "" for a plain SQL-only turn
    kind: str            # "sql" | "python"
    code: str
    source_answer_id: str
    is_dependency: bool  # True if pulled in only because a selected turn needs it


@dataclass
class StorylineContent:
    conversation_title: str
    turns: List[StorylineTurn] = field(default_factory=list)
    code_appendix: List[CodeAppendixEntry] = field(default_factory=list)
    estimated_tokens: int = 0
    over_budget: bool = False


def assemble_storyline(conversation: Dict[str, Any], answer_ids: List[str]) -> StorylineContent:
    id_order = {aid: i for i, aid in enumerate(answer_ids)}
    all_messages = conversation.get("messages", [])
    by_id = {m["answer_id"]: m for m in all_messages}
    label_to_message = {
        m["produced_df_label"]: m for m in all_messages if m.get("produced_df_label")
    }

    selected = [m for m in all_messages if m["answer_id"] in id_order]

    turns = [StorylineTurn(
        answer_id=m["answer_id"], question=m["question"], answer=m["answer"],
        facts=list(m.get("facts", [])), caveats=list(m.get("caveats", [])),
        created_at=m.get("created_at", ""),
    ) for m in selected]

    selected_ids = set(id_order)
    appendix: List[CodeAppendixEntry] = []
    dependency_answer_ids_added: set = set()

    for m in selected:
        for q in m.get("queries_run", []):
            appendix.append(CodeAppendixEntry(
                label=m.get("produced_df_label", ""), kind="sql", code=q,
                source_answer_id=m["answer_id"], is_dependency=False))
        for p in m.get("python_cells", []):
            appendix.append(CodeAppendixEntry(
                label=p.get("df_label", ""), kind="python", code=p.get("code", ""),
                source_answer_id=m["answer_id"], is_dependency=False))
            dep_msg = label_to_message.get(p.get("df_label"))
            if (dep_msg is not None
                    and dep_msg["answer_id"] not in selected_ids
                    and dep_msg["answer_id"] not in dependency_answer_ids_added):
                for q in dep_msg.get("queries_run", []):
                    appendix.append(CodeAppendixEntry(
                        label=p.get("df_label", ""), kind="sql", code=q,
                        source_answer_id=dep_msg["answer_id"], is_dependency=True))
                dependency_answer_ids_added.add(dep_msg["answer_id"])

    estimate_text = "\n".join(
        t.question + t.answer + " ".join(t.facts) + " ".join(t.caveats) for t in turns
    ) + "\n".join(e.code for e in appendix)
    estimated_tokens = len(estimate_text) // CHARS_PER_TOKEN_ESTIMATE

    return StorylineContent(
        conversation_title=conversation.get("title", ""),
        turns=turns, code_appendix=appendix,
        estimated_tokens=estimated_tokens,
        over_budget=estimated_tokens > WARN_TOKEN_THRESHOLD,
    )
