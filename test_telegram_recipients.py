"""Offline tests for the optional multi-user notification path."""

import io
import json
import os
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "111")

import monitor


class FakeResponse:
    def __init__(self, payload):
        self.stream = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stream.close()

    def read(self):
        return self.stream.read()


class TelegramSubscribersTest(unittest.TestCase):
    def setUp(self):
        monitor.telegram_recipients.cache_clear()
        self.url = "https://example.workers.dev/subscriber-ids"
        self.patches = [
            patch.object(monitor, "CHAT_ID", "111"),
            patch.object(monitor, "SUBSCRIBERS_URL", self.url),
            patch.object(monitor, "SUBSCRIBERS_TOKEN", "private-test-token"),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.stop_patches)

    def stop_patches(self):
        for item in reversed(self.patches):
            item.stop()
        monitor.telegram_recipients.cache_clear()

    def test_approved_ids_are_deduplicated_and_owner_is_included(self):
        def respond(request, timeout):
            self.assertEqual(request.get_header("Authorization"), "Bearer private-test-token")
            self.assertEqual(timeout, 15)
            return FakeResponse({"chat_ids": ["222", "111", 333, "222"]})

        with patch.object(monitor.urllib.request, "urlopen", side_effect=respond):
            self.assertEqual(monitor.telegram_recipients(), ("111", "222", "333"))
            self.assertEqual(monitor.telegram_recipients(), ("111", "222", "333"))

    def test_subscriber_endpoint_failure_preserves_owner_alerts(self):
        with patch.object(monitor.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            self.assertEqual(monitor.telegram_recipients(), ("111",))

    def test_malformed_subscriber_list_is_not_used(self):
        with patch.object(monitor.urllib.request, "urlopen", return_value=FakeResponse({"chat_ids": ["222", "bad-id"]})):
            self.assertEqual(monitor.telegram_recipients(), ("111",))

    def test_broadcast_continues_after_one_recipient_fails(self):
        sent = []

        def send(request, timeout):
            args = urllib.parse.parse_qs(request.data.decode("utf-8"))
            chat_id = args["chat_id"][0]
            sent.append(chat_id)
            if chat_id == "222":
                raise urllib.error.URLError("blocked bot")
            return FakeResponse({"ok": True})

        with patch.object(monitor, "telegram_recipients", return_value=("111", "222", "333")):
            with patch.object(monitor.urllib.request, "urlopen", side_effect=send):
                monitor.telegram("Board updated")
        self.assertEqual(sent, ["111", "222", "333"])

    def test_broadcast_raises_only_if_all_recipients_fail(self):
        with patch.object(monitor, "telegram_recipients", return_value=("111", "222")):
            with patch.object(monitor.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
                with self.assertRaisesRegex(RuntimeError, "every recipient"):
                    monitor.telegram("Board updated")


if __name__ == "__main__":
    unittest.main()
