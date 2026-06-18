from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class QueryPlan:
    normalized_question: str
    search_queries: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    chat_hints: list[str] = field(default_factory=list)
    preferred_senders: list[str] = field(default_factory=list)
    preferred_chats: list[str] = field(default_factory=list)
    answer_kind: str = "fact"
    time_hint: str = "any"
    resolved_question: str = ""

    def all_queries(self, original_question: str) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        candidates = [self.normalized_question, *self.search_queries]
        if not any(candidate.strip() for candidate in candidates):
            candidates = [original_question]
        for candidate in candidates:
            value = candidate.strip()
            key = value.casefold()
            if not value or key in seen:
                continue
            seen.add(key)
            ordered.append(value)
        return ordered
