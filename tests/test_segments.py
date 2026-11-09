"""Boundary and join checks for long recordings (no microphone or network needed)."""
import asyncio
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch

os.environ['LIVE_DICTATION_INSTANCE'] = 'dev'
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app


class SegmentTests(unittest.TestCase):
    def test_trigger_windows_and_final_tail_cover_every_stop(self):
        self.assertEqual(app.segment_window(0), (50, 0, 40))
        self.assertEqual(app.segment_window(1), (90, 20, 70))
        self.assertEqual(app.segment_window(2), (150, 50, 130))
        self.assertEqual(app.segment_window(7), (450, 350, 430))
        self.assertEqual((app.segment_window(7)[2] - 20, 475), (410, 475))
        self.assertEqual((app.segment_window(7)[2] - 20, 500), (410, 500))
        for stop in (49, 50, 51, 59, 60, 89, 90, 91, 149, 150, 151, 475, 500):
            windows = []
            index = 0
            while app.segment_window(index)[0] <= stop:
                _trigger, start, end = app.segment_window(index)
                windows.append((start, end))
                index += 1
            if not windows:
                windows = [(0, stop)]
            else:
                windows.append((windows[-1][1] - 20, stop))
            self.assertEqual(windows[0][0], 0)
            self.assertEqual(windows[-1][1], stop)
            for left, right in zip(windows, windows[1:]):
                self.assertLessEqual(right[0], left[1])
                self.assertGreater(right[1], left[1])

    def test_spoken_overlap_is_removed(self):
        parts = [(0, 70, 'We should make this work. The quick brown fox jumps over the lazy dog and then'),
                 (50, 100, 'The quick brown fox jumps over the lazy dog and then we continue.')]
        self.assertEqual(app.stitch_segments(parts, b''),
                         'We should make this work. The quick brown fox jumps over the lazy dog and then we continue.')

    def test_silent_overlap_needs_no_matching_words(self):
        parts = [(0, 70, 'First thought.'), (50, 90, 'Another thought.')]
        self.assertEqual(app.stitch_segments(parts, bytes(90 * app.PCM_BYTES_PER_SECOND)),
                         'First thought. Another thought.')

    def test_join_prefers_words_nearest_the_boundary(self):
        parts = [(0, 70, 'alpha beta gamma delta epsilon zeta eta theta iota kappa late one two three four five'),
                 (50, 100, 'alpha beta gamma delta epsilon zeta eta theta iota kappa different one two three four five continuing')]
        self.assertEqual(app.stitch_segments(parts, b''),
                         parts[0][2] + ' continuing')

    def test_short_exact_phrase_at_the_seam_is_joined(self):
        parts = [(0, 70, 'I need to check that. But for...'),
                 (50, 100, 'But for... the next point, we can continue.')]
        self.assertEqual(app.stitch_segments(parts, b''),
                         'I need to check that. But for... the next point, we can continue.')

    def test_uncertain_spoken_overlap_uses_full_audio_fallback(self):
        parts = [(0, 70, 'First thought.'), (50, 90, 'Completely different words.')]
        pcm = (500).to_bytes(2, 'little', signed=True) * (90 * 24000)
        self.assertIsNone(app.stitch_segments(parts, pcm))

    def test_completed_part_has_valid_audio_and_saved_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary)

            class Session:
                def segment_path(self, index, start, end):
                    return folder / f'{index}-{start}-{end}'

                def save_segment_text(self, index, start, end, text):
                    self.segment_path(index, start, end).with_suffix('.txt').write_text(text)

                def event(self, *_args, **_kwargs):
                    pass

            dictation = app.Dictation.__new__(app.Dictation)
            dictation._transcribe_audio_path = lambda _key, _path, timeout: ('Words preserved.', .4)
            audio = bytes(app.PCM_BYTES_PER_SECOND)
            job = dictation._start_segment('key', Session(), 0, 0, 1, audio, early=False)
            self.assertEqual(job['future'].result(timeout=2), ('Words preserved.', .4))
            with wave.open(str(folder / '0-0-1.wav'), 'rb') as recorded:
                self.assertEqual(recorded.getnframes(), 24000)
            self.assertEqual((folder / '0-0-1.txt').read_text(), 'Words preserved.')

    def test_quiet_part_can_have_no_words(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = pathlib.Path(temporary)

            class Session:
                def segment_path(self, *_args):
                    return folder / 'quiet'

                def save_segment_text(self, *_args):
                    (folder / 'quiet.txt').write_text('')

                def event(self, *_args, **_kwargs):
                    pass

            dictation = app.Dictation.__new__(app.Dictation)
            dictation._transcribe_audio_path = lambda _key, _path, timeout: ('', .3)
            job = dictation._start_segment(
                'key', Session(), 0, 0, 1, bytes(app.PCM_BYTES_PER_SECOND), early=False)
            self.assertEqual(job['future'].result(timeout=2), ('', .3))
            self.assertTrue((folder / 'quiet.txt').exists())

    def test_capture_starts_early_parts_without_stopping_audio(self):
        stop = threading.Event()
        jobs = []

        class Microphone:
            returncode = None

            class Output:
                reads = 0

                async def read(self, size):
                    self.reads += 1
                    if self.reads == 1500:
                        stop.set()
                    return bytes(size)

            stdout = Output()

            def terminate(self):
                self.returncode = 0

            async def wait(self):
                return 0

        class Session:
            class Audio:
                def writeframesraw(self, _chunk):
                    pass

            audio = Audio()

        dictation = app.Dictation.__new__(app.Dictation)
        dictation._start_segment = lambda _key, _session, index, start, end, audio, early: (
            jobs.append((index, start, end, len(audio), early)) or {'end': end})

        async def fake_microphone(*_args, **_kwargs):
            return Microphone()

        with patch.object(app.asyncio, 'create_subprocess_exec', fake_microphone), \
             patch.object(app.GLib, 'idle_add', return_value=1):
            pcm = asyncio.run(dictation._capture('key', Session(), stop, []))
        self.assertEqual(len(pcm), 150 * app.PCM_BYTES_PER_SECOND)
        self.assertEqual(jobs, [
            (0, 0, 40, 40 * app.PCM_BYTES_PER_SECOND, True),
            (1, 20, 70, 50 * app.PCM_BYTES_PER_SECOND, True),
            (2, 50, 130, 80 * app.PCM_BYTES_PER_SECOND, True),
        ])

    def test_uncertain_join_is_pasted_for_review_without_enter(self):
        dictation = app.Dictation.__new__(app.Dictation)
        dictation.session = object()
        dictation.live = SimpleNamespace(set_text=lambda _value: None)
        dictation.status = SimpleNamespace(set_text=lambda _value: None)
        dictation.enter_button = SimpleNamespace(get_active=lambda: True)
        dictation.mode = 'cursor'
        dictation.caret_insertion = 'paste'
        dictation.delivery_error = False
        dictation._target_focused = lambda: True
        pressed = []
        dictation._press_enter = lambda: pressed.append(True)
        dictation._paste_target = lambda _session, _message, callback: callback(True, .2, True)
        plan = {'message': 'Text needing review.', 'cue': None,
                'format_requested': False, 'format_error': None,
                'grammar_error': None, 'join_uncertain': True}
        results = []
        dictation._text_event(dictation.session, plan, results.append)
        self.assertEqual(pressed, [])
        self.assertEqual(results[0]['delivery_method'], 'paste')
        self.assertTrue(dictation.delivery_error)


if __name__ == '__main__':
    unittest.main()
