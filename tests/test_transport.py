from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from beeper_bot.beeper_api import BeeperApiClient, make_message_client
from beeper_bot.config import BeeperConfig, load_config
from beeper_bot.db import init_db_path, open_db
from beeper_bot.matrix_transport import MatrixTransport


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


class ReadOnlyTransportTest(unittest.TestCase):
    def _assert_outbound_empty(self, config) -> None:
        init_db_path(config.archive.path)
        with open_db(config.archive.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbound_queue").fetchone()[0], 0)

    def test_desktop_send_is_denied_by_default(self) -> None:
        config = load_config(_write_config(""))
        client = make_message_client(config.beeper, allow_send=config.security.allow_send)
        with self.assertRaisesRegex(PermissionError, "sending disabled"):
            client.send_message("chat", "hello")
        self._assert_outbound_empty(config)

    def test_matrix_send_is_denied_by_default(self) -> None:
        config = load_config(_write_config(""))
        client = MatrixTransport.__new__(MatrixTransport)
        client.allow_send = config.security.allow_send
        with self.assertRaisesRegex(PermissionError, "sending disabled"):
            client.send_message("chat", "hello")
        self._assert_outbound_empty(config)

    def test_explicit_allow_send_enables_desktop_send(self) -> None:
        config = load_config(_write_config("[security]\nallow_send = true\n"))
        client = make_message_client(config.beeper, allow_send=config.security.allow_send)
        client._request = Mock()
        client.send_message("chat", "hello")
        client._request.assert_called_once_with("POST", "/chats/chat/messages", {"text": "hello"})


if __name__ == "__main__":
    unittest.main()
