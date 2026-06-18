from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .db import open_db, utc_now


@dataclass(slots=True)
class PersonEntry:
    person_id: str
    canonical_name: str
    aliases: list[str]
    chat_ids: list[str]


@dataclass(slots=True)
class PersonGraph:
    people: list[PersonEntry]

    def find_person(self, name: str) -> PersonEntry | None:
        key = name.strip().casefold()
        if not key:
            return None
        for person in self.people:
            if person.canonical_name.casefold() == key:
                return person
            for alias in person.aliases:
                if alias.casefold() == key:
                    return person
        return None

    def find_people(self, names: list[str]) -> list[PersonEntry]:
        found: list[PersonEntry] = []
        seen: set[str] = set()
        for name in names:
            person = self.find_person(name)
            if person and person.person_id not in seen:
                seen.add(person.person_id)
                found.append(person)
        return found

    def all_chat_ids(self) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for person in self.people:
            for chat_id in person.chat_ids:
                if chat_id not in seen:
                    seen.add(chat_id)
                    ordered.append(chat_id)
        return ordered


def load_person_graph(config: AppConfig) -> PersonGraph:
    people: list[PersonEntry] = []
    with open_db(config.archive.path) as conn:
        rows = conn.execute(
            """
            SELECT
                p.person_id,
                p.canonical_name,
                GROUP_CONCAT(DISTINCT a.alias) AS aliases,
                GROUP_CONCAT(DISTINCT pc.chat_id) AS chat_ids
            FROM people AS p
            LEFT JOIN person_aliases AS a ON a.person_id = p.person_id
            LEFT JOIN person_chats AS pc ON pc.person_id = p.person_id
            GROUP BY p.person_id
            ORDER BY p.canonical_name ASC
            """
        ).fetchall()
        for row in rows:
            people.append(
                PersonEntry(
                    person_id=str(row["person_id"]),
                    canonical_name=str(row["canonical_name"]),
                    aliases=[value.strip() for value in (str(row["aliases"] or "")).split(",") if value.strip()],
                    chat_ids=[value.strip() for value in (str(row["chat_ids"] or "")).split(",") if value.strip()],
                )
            )
    return PersonGraph(people=people)


def upsert_person(config: AppConfig, person_id: str, canonical_name: str) -> None:
    now = utc_now()
    with open_db(config.archive.path) as conn:
        conn.execute(
            """
            INSERT INTO people(person_id, canonical_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(person_id) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                updated_at = excluded.updated_at
            """,
            (person_id, canonical_name, now, now),
        )
        conn.commit()


def add_person_alias(config: AppConfig, person_id: str, alias: str) -> None:
    alias = alias.strip()
    if not alias:
        return
    with open_db(config.archive.path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO person_aliases(person_id, alias) VALUES (?, ?)",
            (person_id, alias),
        )
        conn.commit()


def add_person_chat(config: AppConfig, person_id: str, chat_id: str) -> None:
    chat_id = chat_id.strip()
    if not chat_id:
        return
    with open_db(config.archive.path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO person_chats(person_id, chat_id) VALUES (?, ?)",
            (person_id, chat_id),
        )
        conn.commit()


def delete_person(config: AppConfig, person_id: str) -> None:
    with open_db(config.archive.path) as conn:
        conn.execute("DELETE FROM person_chats WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM person_aliases WHERE person_id = ?", (person_id,))
        conn.execute("DELETE FROM people WHERE person_id = ?", (person_id,))
        conn.commit()


def clear_person_aliases(config: AppConfig, person_id: str) -> None:
    with open_db(config.archive.path) as conn:
        conn.execute("DELETE FROM person_aliases WHERE person_id = ?", (person_id,))
        conn.commit()


def clear_person_chats(config: AppConfig, person_id: str) -> None:
    with open_db(config.archive.path) as conn:
        conn.execute("DELETE FROM person_chats WHERE person_id = ?", (person_id,))
        conn.commit()


def seed_person(config: AppConfig, person_id: str, canonical_name: str, aliases: list[str] | None = None, chat_ids: list[str] | None = None) -> PersonEntry:
    upsert_person(config, person_id, canonical_name)
    clear_person_aliases(config, person_id)
    for alias in aliases or []:
        add_person_alias(config, person_id, alias)
    clear_person_chats(config, person_id)
    for chat_id in chat_ids or []:
        add_person_chat(config, person_id, chat_id)
    return PersonEntry(
        person_id=person_id,
        canonical_name=canonical_name,
        aliases=list(aliases or []),
        chat_ids=list(chat_ids or []),
    )
