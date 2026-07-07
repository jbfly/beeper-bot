from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from beeper_bot.beeper_api import BeeperApiClient, make_message_client
from beeper_bot.config import BeeperConfig, load_config


def _write_config(body: str) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "config.toml"
    tmp.write_text(body)
    return tmp


class TransportConfigTest(unittest.TestCase):
    def test_transport_defaults_to_desktop_api(self) -> None:
        config = load_config(_write_config('[beeper]\ncontrol_chat_id = "x"\n'))
        self.assertEqual(config.beeper.transport, "desktop-api")
        self.assertIsNone(config.beeper.matrix_credentials_file)

    def test_transport_matrix_parsed_with_paths(self) -> None:
        config = load_config(
            _write_config(
                '[beeper]\n'
                'control_chat_id = "x"\n'
                'transport = "matrix"\n'
                'matrix_credentials_file = "/tmp/creds.json"\n'
                'matrix_store_path = "/tmp/store"\n'
            )
        )
        self.assertEqual(config.beeper.transport, "matrix")
        self.assertEqual(config.beeper.matrix_credentials_file, Path("/tmp/creds.json"))
        self.assertEqual(config.beeper.matrix_store_path, Path("/tmp/store"))

    def test_factory_returns_desktop_client_by_default(self) -> None:
        # desktop-api must not import matrix-nio (keeps the default runtime
        # dependency-free); this succeeds even where nio is unavailable.
        client = make_message_client(BeeperConfig(transport="desktop-api"))
        self.assertIsInstance(client, BeeperApiClient)


if __name__ == "__main__":
    unittest.main()
