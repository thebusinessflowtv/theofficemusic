"""Regression checks for chat skip cooldown and state isolation."""
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from chat_requests import process_chat_message


class TestRequests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for slot in ("twitch", "kick"):
            (self.root / slot).mkdir()
            (self.root / slot / "health.json").write_text('{"status":"live"}')
            (self.root / slot / "desired.json").write_text('{"desired":"live"}')
            (self.root / slot / "now-playing.json").write_text('{"title":"Track 1","artists":"Peter"}')

    def tearDown(self):
        self.temp.cleanup()

    def req(self, plat="twitch", user="alice", mid="m1", msg="!skip", t=1000):
        return process_chat_message(self.root, platform=plat, user_id=user, message_id=mid, text=msg, now=t)

    def test_accept_and_dedupe(self):
        self.assertEqual(self.req()["status"], "accepted")
        self.assertEqual(len(list((self.root / "twitch" / "audio-commands").glob("*.json"))), 1)
        self.assertEqual(self.req()["status"], "duplicate")

    def test_individual_and_global_cooldown(self):
        self.req()
        self.assertEqual(self.req(user="bob", mid="m2", t=1059)["status"], "cooldown")
        self.assertEqual(self.req(user="bob", mid="m3", t=1060)["status"], "accepted")
        self.assertEqual(self.req(user="alice", mid="m4", t=1061)["status"], "cooldown")

    def test_isolated_platforms(self):
        self.assertEqual(self.req()["status"], "accepted")
        self.assertEqual(self.req(plat="kick")["status"], "accepted")

    def test_normal_chat_unrestricted(self):
        self.assertEqual(self.req(msg="hello")["status"], "ignored")
        self.assertEqual(self.req(msg="!song")["status"], "now_playing")
        self.assertEqual(self.req()["status"], "accepted")

    def test_no_skip_when_offline(self):
        (self.root / "twitch" / "health.json").write_text('{"status":"stopped"}')
        self.assertEqual(self.req()["status"], "not_live")
        self.assertFalse(list((self.root / "twitch").glob("audio-commands/*.json")))


if __name__ == "__main__":
    unittest.main()
