"""PeterLofi Twitch bot 90-minute interaction regression tests."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"app"))
import twitch_chatbot as bot


class TestTwitchHourly(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)
        (self.path/"twitch").mkdir()
        (self.path/"twitch"/"health.json").write_text('{"status":"live"}')
        (self.path/"twitch"/"desired.json").write_text('{"desired":"live"}')
        self.local=patch.object(bot,"STATE",self.path)
        self.local.start()

    async def asyncTearDown(self):
        self.local.stop()
        self.temp.cleanup()

    async def test_first_90_minutes_delayed_and_persisted(self):
        with patch.object(bot,"send",new_callable=AsyncMock,return_value=True) as sent:
            self.assertEqual(await bot.hourly_interaction_tick(now=1000),"scheduled")
            sent.assert_not_called()
            self.assertEqual(await bot.hourly_interaction_tick(now=6399),"waiting")
            sent.assert_not_called()
            self.assertEqual(await bot.hourly_interaction_tick(now=6400),"sent")
            self.assertEqual(sent.call_count,1)
            self.assertEqual(await bot.hourly_interaction_tick(now=6400),"waiting")
            self.assertEqual(await bot.hourly_interaction_tick(now=11800),"sent")
            self.assertEqual(sent.call_count,2)
            last=json.loads((self.path/"twitch"/"chat-hourly-interaction.json").read_text())
            self.assertEqual(last["next_message_index"],2)
            # Verify new process sees persisted time and doesn't announce on boot.
            self.assertEqual(await bot.hourly_interaction_tick(now=11801),"waiting")

    async def test_offline_no_messages(self):
        with patch.object(bot,"send",new_callable=AsyncMock,return_value=True) as sent:
            self.assertEqual(await bot.hourly_interaction_tick(now=1000),"scheduled")
            (self.path/"twitch"/"health.json").write_text('{"status":"offline"}')
            self.assertEqual(await bot.hourly_interaction_tick(now=7000),"offline")
            sent.assert_not_called()
            (self.path/"twitch"/"health.json").write_text('{"status":"live"}')
            self.assertEqual(await bot.hourly_interaction_tick(now=7000),"sent")
            sent.assert_awaited_once()

    async def test_send_failure_does_not_consume_interval(self):
        with patch.object(bot,"send",new_callable=AsyncMock,return_value=False) as sent:
            await bot.hourly_interaction_tick(now=1000)
            self.assertEqual(await bot.hourly_interaction_tick(now=6400),"retry")
            self.assertEqual(await bot.hourly_interaction_tick(now=6415),"retry")
            self.assertEqual(sent.await_count,2)
        with patch.object(bot,"send",new_callable=AsyncMock,return_value=True):
            self.assertEqual(await bot.hourly_interaction_tick(now=6430),"sent")

    def test_english_and_no_180_second_bot_prompts(self):
        for i in range(12):
            message=bot.choose_hourly_message(i)
            self.assertLess(len(message),430)
            self.assertTrue(message)
            self.assertNotIn("3 minutes",message)
            self.assertNotIn("every 180",message)
        self.assertEqual(bot.choose_hourly_message(0),bot.choose_hourly_message(len(bot.HOURLY_MESSAGES)))


if __name__=="__main__":
    unittest.main()
