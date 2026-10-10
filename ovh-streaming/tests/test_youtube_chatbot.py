"""Isolated YouTube chat adapter regression tests (no credentials or network needed)."""
import json
import tempfile
import unittest
from pathlib import Path

import chat_requests as core
import youtube_chatbot as youtube


class YouTubeMusicCommands(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        for slot in youtube.SLOTS:
            state=self.root/slot
            state.mkdir()
            (state/"health.json").write_text('{"status":"live"}')
            (state/"desired.json").write_text('{"desired":"live"}')
            (state/"now-playing.json").write_text(json.dumps({
                "track_id":"track_"+slot,"title":"Test track "+slot,
            }))

    def tearDown(self):
        self.tmp.cleanup()

    def test_command_isolation_and_120s_cooldown(self):
        hip="youtube-lofi-hip-hop"
        gta="youtube-gta-vi"
        r=core.process_chat_message(self.root,platform=hip,user_id="viewer",
            message_id="msg1",text="!song",now=1000)
        self.assertEqual(r["status"],"now_playing")
        r=core.process_chat_message(self.root,platform=hip,user_id="viewer",
            message_id="msg2",text="!skip",now=1010)
        self.assertEqual(r["status"],"cooldown")
        r=core.process_chat_message(self.root,platform=gta,user_id="viewer",
            message_id="msg3",text="!skip",now=1010)
        self.assertEqual(r["status"],"accepted")
        self.assertEqual(list((self.root/gta/"audio-commands").glob("*.json"))[0].name.endswith(".json"),True)
        self.assertFalse((self.root/hip/"audio-commands").exists())
        r=core.process_chat_message(self.root,platform=hip,user_id="viewer",
            message_id="msg4",text="!freeze",now=1121)
        self.assertEqual(r["status"],"frozen_now")
        blocked=core.process_chat_message(self.root,platform=hip,user_id="someone_else",
            message_id="msg5",text="!skip",now=1122)
        self.assertEqual(blocked["status"],"frozen")
        self.assertTrue(core.clear_freeze_after_track_change(self.root,hip,"track_"+hip))
        next_=core.process_chat_message(self.root,platform=hip,user_id="someone_else",
            message_id="msg6",text="!song",now=1123)
        self.assertEqual(next_["status"],"now_playing")

    def test_authenticated_broadcast_identity_guard(self):
        old_video=youtube.VIDEO_ID
        old_request=youtube.youtube
        try:
            youtube.VIDEO_ID=youtube.SLOTS["youtube-lofi-hip-hop"]
            def fake(method,path,params=None,payload=None):
                if path=="channels":return {"items":[{"id":youtube.CHANNEL_ID}]}
                if path=="liveBroadcasts":
                    return {"items":[{"status":{"lifeCycleStatus":"live"},
                                      "snippet":{"liveChatId":"verified_live_chat"}}]}
                raise AssertionError(path)
            youtube.youtube=fake
            self.assertEqual(youtube.confirmed_chat(),"verified_live_chat")
            def wrong(method,path,params=None,payload=None):
                if path=="channels":return {"items":[{"id":"wrong-channel"}]}
                return fake(method,path,params,payload)
            youtube.youtube=wrong
            with self.assertRaisesRegex(RuntimeError,"youtube_wrong_channel"):
                youtube.confirmed_chat()
        finally:
            youtube.VIDEO_ID=old_video
            youtube.youtube=old_request


if __name__=="__main__":
    unittest.main()
