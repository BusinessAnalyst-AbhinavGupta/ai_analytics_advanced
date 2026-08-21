"""Plan B Task 3 -- sequence selected turns into a document that reads as one
argument.

`assemble_storyline` decides *what* goes into an export and resolves the code
appendix's cross-turn dependencies. It does not narrate, and the difference
shows: eight selected turns come out as eight disconnected question-and-answer
blocks, with the same truncation caveat repeated six times and nothing joining
them. Ordering by the sequence the questions were asked gives you the order of a
person exploring; a reader needs the order of an argument.

Everything here is a pure function of (content, llm). No I/O, no store access.

Three guards do most of the work, and they exist because this is the artefact
someone carries into a meeting to defend a number:

  Provenance. Every section has to cite answer_ids that are really in the export.
  The model is told to attach them; ids it invents are stripped here, and a
  section left citing nothing is dropped rather than published unsourced.

  Figures. Every number-shaped token in the generated prose must already appear
  somewhere in the source turns. A section containing an invented figure is
  dropped and logged, because a storyline with a fabricated number in it is
  worse than no storyline.

  Caveats. These are unioned from the turns in code and de-duplicated on a
  normalised key. The model never sees them and never gets the chance to drop
  one while summarising -- a deliberate strengthening of the plan, which routed
  caveats through the prompt. Losing "this figure is not a defined metric"
  during a tidy-up is exactly the failure this system exists to prevent.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .storyline import StorylineContent

logger = logging.getLogger(__name__)

# A number-shaped token: 1,000,000 / 61.4 / 412003 / 12. Percent signs and
# currency are left out on purpose -- the digits are the claim.
_NUMBER = re.compile(r"\d[\d,]*\.?\d*")

SYSTEM_PROMPT = (
    "You are an analyst writing the narrative for a report that a stakeholder "
    "will present. You are given the turns of an analytical conversation, each "
    "with an id. Sequence them into an argument.\n"
    "Rules you must follow:\n"
    "1. Order sections by the logic of the argument, not by the order the "
    "questions were asked.\n"
    "2. Every section must list the answer_ids of the turns it draws on.\n"
    "3. Use ONLY figures that appear verbatim in the turns. Do not compute, "
    "round, or infer new numbers. If you are unsure of a figure, leave it out "
    "and describe the direction instead.\n"
    "4. Do not write caveats or limitations; they are added separately.\n"
    "Return ONLY a JSON object, no prose around it, of the form:\n"
    '{"title": str, "executive_summary": str, "sections": '
    '[{"heading": str, "body": str, "answer_ids": [str]}]}'
)


@dataclass
class NarratedSection:
    heading: str
    body: str
    answer_ids: List[str] = field(default_factory=list)
    chart_spec: Optional[Dict[str, Any]] = None


@dataclass
class NarratedStoryline:
    title: str = ""
    executive_summary: str = ""       # 3-5 sentences: the answer, up front
    sections: List[NarratedSection] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    ok: bool = True
    error: str = ""


def _caveat_key(text: str) -> str:
    """Normalise a caveat so five turns carrying the same one merge into one.

    Whitespace is collapsed and digits are flattened to `#`, so "truncated at
    1,000,000 rows" and "truncated at 250,000 rows" are recognised as the same
    warning about the same thing.
    """
    collapsed = " ".join((text or "").split()).lower()
    return re.sub(r"\d[\d,]*\.?\d*", "#", collapsed)


def merge_caveats(content: StorylineContent) -> List[str]:
    """Union of every selected turn's caveats, first wording wins.

    Order is preserved so the reader meets them in the order the analysis did.
    """
    out: List[str] = []
    seen = set()
    for turn in content.turns:
        for caveat in turn.caveats:
            key = _caveat_key(caveat)
            if key and key not in seen:
                seen.add(key)
                out.append(caveat)
    return out


def _source_numbers(content: StorylineContent) -> set:
    """Every number-shaped token that legitimately exists in the export."""
    corpus = " ".join(
        [content.conversation_title]
        + [t.question for t in content.turns]
        + [t.answer for t in content.turns]
        + [f for t in content.turns for f in t.facts]
        + [c for t in content.turns for c in t.caveats])
    return {m.group().rstrip(".").replace(",", "") for m in _NUMBER.finditer(corpus)}


def _invented_numbers(text: str, allowed: set) -> List[str]:
    found = {m.group().rstrip(".") for m in _NUMBER.finditer(text or "")}
    return sorted(n for n in found if n.replace(",", "") not in allowed)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Models fence their JSON, or preface it. Take the outermost object."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _llm_live(llm: Any) -> bool:
    return llm is not None and getattr(llm, "name", "null") != "null"


def narrate(content: StorylineContent, llm: Any) -> NarratedStoryline:
    """One pass over the whole assembled content.

    Deliberately not per-turn: sequencing and de-duplication are global
    properties, and a per-turn pass cannot know that turns 2 and 5 make the same
    point. Never raises -- an export must not fail because a model call did.
    """
    caveats = merge_caveats(content)

    if not content.turns:
        return NarratedStoryline(title=content.conversation_title, caveats=caveats,
                                 ok=False, error="nothing selected to narrate")
    if not _llm_live(llm):
        return NarratedStoryline(title=content.conversation_title, caveats=caveats,
                                 ok=False, error="no live model configured")

    payload = {
        "title": content.conversation_title,
        "turns": [{"answer_id": t.answer_id, "question": t.question,
                   "answer": t.answer, "facts": t.facts} for t in content.turns],
    }
    try:
        res = llm.generate(prompt=json.dumps(payload, ensure_ascii=False),
                           system_prompt=SYSTEM_PROMPT, temperature=0.0)
    except Exception as exc:                     # noqa: BLE001 -- degrade, never raise
        logger.warning("storyline narration failed: %s", exc, exc_info=True)
        return NarratedStoryline(title=content.conversation_title, caveats=caveats,
                                 ok=False, error=str(exc))

    parsed = _extract_json(getattr(res, "text", "") or "")
    if parsed is None:
        logger.warning("storyline narration returned no usable JSON")
        return NarratedStoryline(title=content.conversation_title, caveats=caveats,
                                 ok=False, error="the model returned no usable JSON")

    known = {t.answer_id for t in content.turns}
    allowed = _source_numbers(content)

    sections: List[NarratedSection] = []
    for raw in parsed.get("sections") or []:
        if not isinstance(raw, dict):
            continue
        ids = [i for i in (raw.get("answer_ids") or []) if i in known]
        if not ids:
            # Unsourced prose. Provenance is the product, so this is dropped
            # rather than published with a plausible-looking citation.
            logger.warning("dropping narrative section %r: cites no known answer_id",
                           raw.get("heading", ""))
            continue
        body = str(raw.get("body") or "")
        invented = _invented_numbers(body, allowed)
        if invented:
            logger.warning("dropping narrative section %r: figures %s appear in no "
                           "selected turn", raw.get("heading", ""), invented)
            continue
        sections.append(NarratedSection(heading=str(raw.get("heading") or ""),
                                        body=body, answer_ids=ids))

    summary = str(parsed.get("executive_summary") or "")
    if _invented_numbers(summary, allowed):
        logger.warning("dropping the executive summary: it contains figures that "
                       "appear in no selected turn")
        summary = ""

    if not sections:
        return NarratedStoryline(title=content.conversation_title, caveats=caveats,
                                 ok=False,
                                 error="no section survived provenance and figure checks")

    return NarratedStoryline(
        title=str(parsed.get("title") or content.conversation_title),
        executive_summary=summary, sections=sections, caveats=caveats, ok=True)
