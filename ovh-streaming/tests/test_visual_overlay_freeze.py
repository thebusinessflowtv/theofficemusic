import json
import tempfile
import unittest
from pathlib import Path

from app.visual_engine import VisualEngine


class LiveOverlayFreezeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.engine = object.__new__(VisualEngine)
        self.engine.state = self.state
        self.engine.now_playing_overlay_enabled = True
        self.engine.now_playing_overlay_path = self.state / "now-playing-overlay.txt"
        self.engine.now_playing_frozen_title_path = self.state / "now-playing-frozen-title.txt"
        self.engine.now_playing_lock_path = self.state / "now-playing-lock-icon.txt"
        self.write_song("a", "Velvet Orbit")

    def write_song(self, track_id, title):
        (self.state / "now-playing.json").write_text(
            json.dumps({"track_id": track_id, "title": title, "artists": ""}), encoding="utf-8"
        )

    def write_freeze(self, track_id):
        (self.state / "chat-bot-state.json").write_text(
            json.dumps({"freeze": {"track_id": track_id, "user_id": "viewer"}}), encoding="utf-8"
        )

    def content(self, filename):
        return (self.state / filename).read_text(encoding="utf-8").strip()

    def test_unfrozen_song_shows_title_without_padlock(self):
        self.engine.sync_now_playing_overlay()
        self.assertEqual(self.content("now-playing-overlay.txt"), "Velvet Orbit")
        self.assertEqual(self.content("now-playing-frozen-title.txt"), "")
        self.assertEqual(self.content("now-playing-lock-icon.txt"), "")

    def test_freeze_is_track_specific_and_removed_on_change(self):
        self.write_freeze("a")
        self.engine.sync_now_playing_overlay()
        self.assertEqual(self.content("now-playing-overlay.txt"), "")
        self.assertEqual(self.content("now-playing-frozen-title.txt"), "Velvet Orbit")
        self.assertEqual(self.content("now-playing-lock-icon.txt"), "🔒")
        self.write_song("b", "Infinity Trail")
        self.engine.sync_now_playing_overlay()
        self.assertEqual(self.content("now-playing-overlay.txt"), "Infinity Trail")
        self.assertEqual(self.content("now-playing-frozen-title.txt"), "")
        self.assertEqual(self.content("now-playing-lock-icon.txt"), "")


if __name__ == "__main__":
    unittest.main()
