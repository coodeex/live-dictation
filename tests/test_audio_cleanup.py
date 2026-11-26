"""Audio expiry checks use temporary files only; never touch real history."""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ['LIVE_DICTATION_INSTANCE'] = 'dev'
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app


class AudioCleanupTests(unittest.TestCase):
    NOW = 1_700_000_000

    def make_file(self, path, age_seconds, contents=b'audio'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        stamp = self.NOW - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_old_audio_expires_but_text_and_recent_audio_remain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary) / 'history'
            session = root / 'session-one'
            old = app.AUDIO_RETENTION_SECONDS + 1
            current = app.AUDIO_RETENTION_SECONDS
            full = self.make_file(session / 'audio.wav', old, b'12345')
            segment = self.make_file(session / 'segments' / '001.wav', old, b'123')
            mp3 = self.make_file(session / 'segments' / '002.mp3', old, b'12')
            recent = self.make_file(session / 'segments' / '003.wav', current)
            transcript = self.make_file(session / 'transcript.txt', old, b'keep text')
            segment_text = self.make_file(session / 'segments' / '001.txt', old)
            metadata = self.make_file(session / 'metadata.json', old)
            events = self.make_file(session / 'events.jsonl', old)

            self.assertEqual(app.prune_old_audio(root, now=self.NOW), (3, 10, 0))
            for path in (full, segment, mp3):
                self.assertFalse(path.exists())
            for path in (recent, transcript, segment_text, metadata, events):
                self.assertTrue(path.exists())
            self.assertTrue(session.is_dir())

    def test_active_session_and_symlinked_paths_are_not_touched(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = pathlib.Path(temporary)
            root = base / 'history'
            old = app.AUDIO_RETENTION_SECONDS + 1
            active = self.make_file(root / 'active' / 'audio.wav', old)
            expired = self.make_file(root / 'finished' / 'audio.wav', old)
            outside = self.make_file(base / 'outside' / 'audio.wav', old)
            (root / 'linked-session').symlink_to(outside.parent, target_is_directory=True)
            (root / 'finished' / 'linked.wav').symlink_to(outside)
            unrelated = self.make_file(root / 'finished' / 'notes.bin', old)

            result = app.prune_old_audio(root, now=self.NOW,
                                         protected_session=active.parent)
            self.assertEqual(result, (1, 5, 0))
            self.assertTrue(active.exists())
            self.assertFalse(expired.exists())
            self.assertTrue(outside.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue((root / 'finished' / 'linked.wav').is_symlink())

    def test_cleanup_starts_on_worker_only_after_recording_finishes(self):
        dictation = app.Dictation.__new__(app.Dictation)
        dictation.stop_event = object()
        dictation.audio_cleanup_timer = 1
        dictation.audio_cleanup_running = False
        with patch.object(app.threading, 'Thread') as thread:
            self.assertTrue(dictation._start_audio_cleanup())
            thread.assert_not_called()
            dictation.stop_event = None
            self.assertFalse(dictation._start_audio_cleanup())
            thread.return_value.start.assert_called_once()
            self.assertTrue(dictation.audio_cleanup_running)


if __name__ == '__main__':
    unittest.main()
