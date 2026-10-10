import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from bot_conversation import plan_chat_reply


class TestConversation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.state=Path(self.temp.name)
        for slot in ("twitch","kick"):
            folder=self.state/slot
            folder.mkdir()
            (folder/"now-playing.json").write_text('{"title":"Arcade Nights"}')

    def tearDown(self):
        self.temp.cleanup()

    def reply(self, *, msg="@PeterLofi hello!", plat="twitch",user="alice",mid="m1",now=1000,ai=False):
        return plan_chat_reply(self.state,platform=plat,user_id=user,message_id=mid,text=msg,now=now,ai_enabled=ai)

    def test_direct_mention_is_replied_in_english(self):
        result=self.reply()
        self.assertEqual(result["status"],"reply")
        self.assertIn("PeterLofi Radio",result["text"])

    def test_song_response_reads_local_state(self):
        result=self.reply(msg="@PeterLofi what song is playing?")
        self.assertEqual(result["status"],"reply")
        self.assertIn("Arcade Nights",result["text"])

    def test_help_response(self):
        result=self.reply(msg="@PeterLofi what commands?")
        self.assertIn("!freeze",result["text"])

    def test_ignores_unaddressed_chat_and_commands(self):
        self.assertEqual(self.reply(msg="normal chat")["status"],"not_addressed")
        self.assertEqual(self.reply(msg="!skip")["status"],"ignored_command")

    def test_dedupe_and_spam_prevention(self):
        self.assertEqual(self.reply()["status"],"reply")
        self.assertEqual(self.reply()["status"],"duplicate")
        self.assertEqual(self.reply(mid="m2",now=1100)["status"],"rate_limited")
        self.assertEqual(self.reply(mid="m3",now=1120)["status"],"reply")
        self.assertEqual(self.reply(mid="b",user="bob",now=1121)["status"],"rate_limited")
        self.assertEqual(self.reply(mid="b2",user="bob",now=1150)["status"],"reply")

    def test_platform_isolation_and_ai_opt_in(self):
        self.assertEqual(self.reply()["status"],"reply")
        self.assertEqual(self.reply(plat="kick",ai=True)["status"],"needs_ai")
        self.assertEqual(self.reply(mid="next",now=1120,ai=True)["status"],"needs_ai")


if __name__ == "__main__":
    unittest.main()
