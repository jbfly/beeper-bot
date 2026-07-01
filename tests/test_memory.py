from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from beeper_bot.config import load_config
from beeper_bot.db import get_runtime_state, init_db_path, open_db
from beeper_bot.memory import (
    CONTROL_SUMMARY_KEY,
    CONTROL_SUMMARY_UPTO_KEY,
    SUMMARY_KEEP_RECENT_TURNS,
    load_memory_state,
    maybe_refresh_control_summary,
    record_control_turn,
)


class FakeSummarizer:
    def __init__(self, summary: str = "Running summary: user asked about addresses; Taylor's was answered."):
        self.summary = summary
        self.prompts: list[str] = []

    def summarize_text(self, config, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.summary


class RollingSummaryTest(unittest.TestCase):
    def _config(self):
        tmpdir = tempfile.TemporaryDirectory()
        config_path = Path(tmpdir.name) / "config.toml"
        db_path = Path(tmpdir.name) / "archive.sqlite3"
        config_path.write_text(f'[archive]\npath = "{db_path}"\n')
        config = load_config(config_path)
        init_db_path(config.archive.path)
        return config, tmpdir

    def _seed_turns(self, config, count: int, start: int = 1) -> None:
        for idx in range(start, start + count):
            role = "user" if idx % 2 else "assistant"
            record_control_turn(config, role, f"turn {idx} about topic {idx}")

    def test_no_refresh_until_enough_old_turns(self) -> None:
        config, tmpdir = self._config()
        self.addCleanup(tmpdir.cleanup)
        self._seed_turns(config, SUMMARY_KEEP_RECENT_TURNS + 2)
        fake = FakeSummarizer()
        self.assertIsNone(maybe_refresh_control_summary(config, fake))
        self.assertEqual(fake.prompts, [])

    def test_refresh_folds_old_turns_and_stores_cursor(self) -> None:
        config, tmpdir = self._config()
        self.addCleanup(tmpdir.cleanup)
        self._seed_turns(config, 20)
        fake = FakeSummarizer()
        summary = maybe_refresh_control_summary(config, fake)
        self.assertIsNotNone(summary)
        self.assertIn("Running summary", summary)

        # old turns are in the prompt; the recent verbatim window is not
        self.assertIn("turn 1 ", fake.prompts[0])
        self.assertIn(f"turn {20 - SUMMARY_KEEP_RECENT_TURNS} ", fake.prompts[0])
        self.assertNotIn(f"turn {20 - SUMMARY_KEEP_RECENT_TURNS + 1} ", fake.prompts[0])

        with open_db(config.archive.path) as conn:
            self.assertEqual(get_runtime_state(conn, CONTROL_SUMMARY_KEY), fake.summary)
            self.assertEqual(get_runtime_state(conn, CONTROL_SUMMARY_UPTO_KEY), str(20 - SUMMARY_KEEP_RECENT_TURNS))

        # no second refresh until another batch of old turns accumulates
        self.assertIsNone(maybe_refresh_control_summary(config, fake))
        self.assertEqual(len(fake.prompts), 1)

    def test_second_refresh_folds_only_new_old_turns(self) -> None:
        config, tmpdir = self._config()
        self.addCleanup(tmpdir.cleanup)
        self._seed_turns(config, 20)
        fake = FakeSummarizer()
        maybe_refresh_control_summary(config, fake)
        self._seed_turns(config, 8, start=21)
        summary = maybe_refresh_control_summary(config, fake)
        self.assertIsNotNone(summary)
        self.assertEqual(len(fake.prompts), 2)
        self.assertNotIn("turn 1 ", fake.prompts[1])
        self.assertIn("Previous summary:\nRunning summary", fake.prompts[1])

    def test_load_memory_state_uses_stored_summary(self) -> None:
        config, tmpdir = self._config()
        self.addCleanup(tmpdir.cleanup)
        self._seed_turns(config, 20)
        maybe_refresh_control_summary(config, FakeSummarizer())
        state = load_memory_state(config)
        self.assertIn("Running summary", state["control_summary"])


if __name__ == "__main__":
    unittest.main()
