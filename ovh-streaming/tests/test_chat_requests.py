"""Regression tests for verified Twitch and Kick chat commands."""
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from chat_requests import process_chat_message, clear_freeze_after_track_change


class TestChatRequests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for slot in ("twitch", "kick"):
            (self.root / slot).mkdir()
            (self.root / slot / "health.json").write_text('{"status":"live"}')
            (self.root / slot / "desired.json").write_text('{"desired":"live"}')
            (self.root / slot / "now-playing.json").write_text(
                '{"track_id":"t1","title":"Track One","artists":"Peter"}')

    def tearDown(self):
        self.temp.cleanup()

    def req(self, plat="twitch", user="alice", mid="m1", msg="!skip", t=1000):
        return process_chat_message(self.root, platform=plat,
                                    user_id=user, message_id=mid, text=msg, now=t)

    def change_song(self, plat, track_id="t2"):
        (self.root / plat / "now-playing.json").write_text(
            f'{{"track_id":"{track_id}","title":"Track Two","artists":"Peter"}}')

    def test_skip_queue_and_duplicate(self):
        self.assertEqual(self.req()["action"], "skip")
        self.assertEqual(self.req()["status"], "duplicate")
        self.assertEqual(len(list((self.root / "twitch" / "audio-commands").glob("*.json"))), 1)

    def test_twitch_no_additional_user_cooldown(self):
        self.assertEqual(self.req()["status"], "accepted")
        self.assertEqual(self.req(mid="m2", msg="!song", t=1001)["status"], "now_playing")
        self.assertEqual(self.req(mid="m3", msg="!freeze", t=1002)["status"], "frozen_now")
        # The frozen song still protects against a skip from anyone.
        self.assertEqual(self.req(mid="m4", msg="!skip", t=1003)["status"], "frozen")

    def test_kick_retains_cooldown_until_kick_chat_is_configured(self):
        self.assertEqual(self.req(plat="kick")["status"], "accepted")
        self.assertEqual(self.req(plat="kick", mid="m2", msg="!song", t=1179)["status"], "cooldown")
        self.assertEqual(self.req(plat="kick", mid="m3", msg="!song", t=1180)["status"], "now_playing")

    def test_global_60s_for_skip_and_back(self):
        self.assertEqual(self.req()["status"], "accepted")
        self.assertEqual(self.req(user="bob", mid="b", t=1059, msg="!back")["status"], "station_cooldown")
        self.assertEqual(self.req(user="bob", mid="c", t=1060, msg="!back")["action"], "previous")

    def test_freeze_blocks_skip_back_for_everyone(self):
        self.assertEqual(self.req(msg="!freeze")["status"], "frozen_now")
        self.assertEqual(self.req(user="bob", mid="b", t=1010, msg="!skip")["status"], "frozen")
        self.assertEqual(self.req(user="bob", mid="c", t=1010, msg="!back")["status"], "frozen")
        self.assertEqual(self.req(user="bob", mid="d", t=1010, msg="!freeze")["status"], "already_frozen")
        self.assertFalse(list((self.root / "twitch").glob("audio-commands/*.json")))

    def test_freeze_expires_on_natural_song_end_no_twitch_bot_cooldown(self):
        self.assertEqual(self.req(msg="!freeze")["status"], "frozen_now")
        self.change_song("twitch")
        self.assertTrue(clear_freeze_after_track_change(self.root, "twitch", "t1"))
        self.assertFalse(clear_freeze_after_track_change(self.root, "twitch", "t1"))
        self.assertEqual(self.req(msg="!skip", user="bob", mid="sk2", t=1010)["status"], "accepted")
        self.assertEqual(self.req(msg="!freeze", mid="f2", t=1010)["status"], "frozen_now")
        self.assertEqual(self.req(msg="!freeze", mid="f3", t=1180)["status"], "already_frozen")

    def test_stale_freeze_auto_expires_when_song_changes(self):
        self.req(msg="!freeze")
        self.change_song("twitch")
        self.assertEqual(self.req(msg="!back", user="bob", mid="b", t=1020)["status"], "accepted")

    def test_platforms_isolated(self):
        self.assertEqual(self.req(msg="!freeze")["status"], "frozen_now")
        self.assertEqual(self.req(plat="kick")["status"], "accepted")
        self.assertEqual(self.req(user="bob", mid="b", msg="!back", t=1200)["status"], "frozen")

    def test_only_four_english_commands(self):
        for word in ("!pular", "!musica", "!música", "!previous", "hello"):
            self.assertEqual(self.req(msg=word)["status"], "ignored")
        self.assertEqual(self.req(msg="!song")["status"], "now_playing")

    def test_no_actions_while_offline(self):
        (self.root / "twitch" / "health.json").write_text('{"status":"stopped"}')
        self.assertEqual(self.req()["status"], "not_live")
        self.assertFalse(list((self.root / "twitch").glob("audio-commands/*.json")))


if __name__ == "__main__":
    unittest.main()
