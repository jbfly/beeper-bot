from __future__ import annotations

import unittest

from beeper_bot.config import AppConfig, CloudLlmConfig, LlmConfig
from beeper_bot.llm import LlmError, OpenAiCompatLlmClient, _require_local_base_url


def _config(**cloud) -> AppConfig:
    cfg = AppConfig(config_path=__import__("pathlib").Path("/tmp/x"))
    cfg.llm = LlmConfig(base_url="http://192.168.1.11:8090/v1", model="local-model")
    if cloud:
        cfg.cloud_llm = CloudLlmConfig(**cloud)
    return cfg


class LocalGuardTest(unittest.TestCase):
    def test_allows_loopback_and_private_lan(self) -> None:
        for url in ("http://127.0.0.1:8090/v1", "http://localhost:8090/v1", "http://192.168.1.11:8090/v1", "http://10.0.0.5:8090/v1"):
            _require_local_base_url(url)  # must not raise

    def test_rejects_public_address(self) -> None:
        with self.assertRaises(LlmError):
            _require_local_base_url("https://api.openai.com/v1")


class RoutingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = OpenAiCompatLlmClient()
        self.calls: list[tuple[str, dict]] = []

    def _capture(self, cfg):
        # stub the network: record the resolved url + headers instead of POSTing
        import beeper_bot.llm as llm

        orig = llm.request.urlopen
        client = self.client
        recorded = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"choices":[{"message":{"content":"ok"}}]}'

        def fake_urlopen(req, timeout=0):
            recorded["url"] = req.full_url
            recorded["headers"] = dict(req.headers)
            return FakeResp()

        llm.request.urlopen = fake_urlopen
        try:
            client._post_chat(cfg, [{"role": "user", "content": "hi"}], purpose="planner")
        finally:
            llm.request.urlopen = orig
        return recorded

    def test_local_purpose_stays_on_local_tier(self) -> None:
        cfg = _config(base_url="https://api.openai.com/v1", model="gpt", api_key_env="X", purposes=["planner"])
        import os
        os.environ["X"] = "secret"
        # a non-opted-in purpose (answer) must stay local even with cloud configured
        rec = {}
        import beeper_bot.llm as llm

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"choices":[{"message":{"content":"ok"}}]}'

        orig = llm.request.urlopen
        llm.request.urlopen = lambda req, timeout=0: (rec.update(url=req.full_url, auth="Authorization" in dict(req.headers)) or FakeResp())
        try:
            self.client._post_chat(cfg, [{"role": "user", "content": "hi"}], purpose="answer")
        finally:
            llm.request.urlopen = orig
        self.assertIn("192.168.1.11", rec["url"])
        self.assertFalse(rec["auth"])

    def test_opted_in_purpose_routes_to_cloud_with_auth(self) -> None:
        import os
        os.environ["CLOUDKEY"] = "sk-test"
        cfg = _config(base_url="https://api.openai.com/v1", model="gpt", api_key_env="CLOUDKEY", purposes=["planner"])
        rec = self._capture(cfg)
        self.assertIn("api.openai.com", rec["url"])
        self.assertEqual(rec["headers"].get("Authorization"), "Bearer sk-test")

    def test_cloud_purpose_without_key_errors(self) -> None:
        import os
        os.environ.pop("MISSINGKEY", None)
        cfg = _config(base_url="https://api.openai.com/v1", model="gpt", api_key_env="MISSINGKEY", purposes=["planner"])
        with self.assertRaises(LlmError):
            self.client._post_chat(cfg, [{"role": "user", "content": "hi"}], purpose="planner")


if __name__ == "__main__":
    unittest.main()
