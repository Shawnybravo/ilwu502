"""Verify the manual update contains named per-ship audit rows."""
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "123456")
import monitor


class FreshUpdateTests(unittest.TestCase):
    def test_each_available_board_includes_named_ship_audit(self):
        boards = []
        for name in ("Morning Vessel", "Afternoon Vessel", "Night Vessel"):
            boards.append(monitor.calculate_board({"board": {"ships_in_port": [{
                "ships_name": name, "commodities": "CONTAINERS",
                "gangs": "2HT", "jobs": "14LASHERS",
            }]}}, "board"))
        with patch.object(monitor, "telegram") as send:
            monitor.send_fresh_update(None, None, *boards, [], Mock())
        message = send.call_args.args[0]
        for name in ("Morning Vessel", "Afternoon Vessel", "Night Vessel"):
            self.assertIn(f"• {name}: 16 (G 2 + J 14)", message)
        self.assertEqual(message.count("Per-ship audit"), 3)
        self.assertNotIn("Unnamed ship", message)

    def test_unavailable_boards_still_produce_update(self):
        with patch.object(monitor, "telegram") as send:
            monitor.send_fresh_update(None, None, None, None, None, None, Mock())
        message = send.call_args.args[0]
        self.assertEqual(message.count("⚠️ Source unavailable"), 4)
        self.assertNotIn("Per-ship audit", message)
