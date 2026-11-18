"""Exercise delayed X11 clipboard reads on an isolated test display."""
import os
import shutil
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ['LIVE_DICTATION_INSTANCE'] = 'dev'
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app
from gi.repository import Gdk, GLib, Gtk


class PasteChunkTests(unittest.TestCase):
    def test_threshold_and_exact_text(self):
        short = 'a' * app.PASTE_SPLIT_TRIGGER_CHARS
        self.assertEqual(app.paste_chunks(short), [short])
        long = ('Hello, world!  More words.\n' * 220)[:5293]
        pieces = app.paste_chunks(long)
        self.assertGreaterEqual(len(pieces), 3)
        self.assertTrue(all(len(piece) <= app.PASTE_CHUNK_MAX_CHARS for piece in pieces))
        self.assertEqual(''.join(pieces), long)

    def test_unbroken_text_and_unicode_are_preserved(self):
        text = 'ä' * 3701 + ' 👋🏽\n' + 'x' * 2000
        pieces = app.paste_chunks(text)
        self.assertTrue(all(0 < len(piece) <= app.PASTE_CHUNK_MAX_CHARS
                            for piece in pieces))
        self.assertEqual(''.join(pieces), text)

    def test_partial_paste_never_sends_enter(self):
        dictation = app.Dictation.__new__(app.Dictation)
        dictation.session = object()
        dictation.delivery_error = False
        dictation.status = SimpleNamespace(set_text=lambda _text: None)
        dictation._press_enter = lambda: self.fail('Enter must wait for every piece')
        metrics = {}
        completed = []
        dictation._paste_completed(dictation.session, 'long message', True, metrics,
                                   lambda value: completed.append(value), None, 1.2, True)
        self.assertTrue(dictation.delivery_error)
        self.assertEqual(metrics['delivery_method'], 'paste-uncertain')
        self.assertEqual(completed, [metrics])


@unittest.skipUnless(
    os.environ.get('LIVE_DICTATION_ISOLATED_CLIPBOARD_TEST') == '1' and
    shutil.which('xclip') is not None and
    Gdk.Display.get_default() is not None and
    Gdk.Display.get_default().get_name().startswith(':'),
    'requires an explicitly selected isolated X11 display and xclip')
class ClipboardPasteTests(unittest.TestCase):
    def test_long_message_is_pasted_in_order_without_replacing_earlier_pieces(self):
        class SuccessfulShortcut:
            def wait_finish(self, _result):
                return True

            def get_successful(self):
                return True

        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        message = ('One sentence. Another sentence with accents äö.\n' * 120)[:5293]
        pieces = app.paste_chunks(message)
        buffer = Gtk.TextBuffer()
        dictation = app.Dictation.__new__(app.Dictation)
        dictation.session = object()
        dictation.clipboard = clipboard
        dictation.clipboard_snapshot = {
            'session': dictation.session, 'ready': True, 'text': 'previous copy'}
        dictation.paste_transaction = None
        dictation._target_focused = lambda: True
        result = {}
        loop = GLib.MainLoop()
        attempts = []

        def send_shortcut(transaction):
            attempts.append(transaction['value'])
            transaction['attempted'] = True
            buffer.paste_clipboard(clipboard, None, True)
            GLib.timeout_add(20, lambda: (
                dictation._paste_process_done(SuccessfulShortcut(), None, transaction),
                False)[1])

        dictation._send_paste_shortcut = send_shortcut
        with patch.object(app, 'PASTE_CHUNK_SETTLE_MS', 80):
            dictation._paste_target(
                dictation.session, message,
                lambda sent, _seconds, attempted: (
                    result.update(sent=sent, attempted=attempted,
                                  text_at_completion=buffer.get_text(
                                      buffer.get_start_iter(), buffer.get_end_iter(), True)),
                    loop.quit()))
            GLib.timeout_add(4000, lambda: (loop.quit(), False)[1])
            loop.run()

        pasted = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        self.assertEqual(attempts, pieces)
        self.assertEqual(pasted, message)
        self.assertEqual(result.get('text_at_completion'), message)
        self.assertTrue(result.get('sent'))
        self.assertTrue(result.get('attempted'))
        self.assertEqual(clipboard.wait_for_text(), pieces[-1])

    def test_failed_later_piece_reports_partial_paste_without_retrying_full_text(self):
        class Shortcut:
            def __init__(self, successful):
                self.successful = successful

            def wait_finish(self, _result):
                return True

            def get_successful(self):
                return self.successful

        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        message = 'Long text. ' * 400
        buffer = Gtk.TextBuffer()
        dictation = app.Dictation.__new__(app.Dictation)
        dictation.session = object()
        dictation.clipboard = clipboard
        dictation.clipboard_snapshot = {
            'session': dictation.session, 'ready': True, 'text': 'previous copy'}
        dictation.paste_transaction = None
        dictation._target_focused = lambda: True
        result = {}
        loop = GLib.MainLoop()
        attempts = []

        def send_shortcut(transaction):
            attempts.append(transaction['value'])
            transaction['attempted'] = True
            if len(attempts) == 1:
                buffer.paste_clipboard(clipboard, None, True)
            GLib.timeout_add(20, lambda: (
                dictation._paste_process_done(
                    Shortcut(len(attempts) == 1), None, transaction), False)[1])

        dictation._send_paste_shortcut = send_shortcut
        with patch.object(app, 'PASTE_CHUNK_SETTLE_MS', 80):
            dictation._paste_target(
                dictation.session, message,
                lambda sent, _seconds, attempted: (
                    result.update(sent=sent, attempted=attempted), loop.quit()))
            GLib.timeout_add(4000, lambda: (loop.quit(), False)[1])
            loop.run()

        self.assertEqual(len(attempts), 2)
        self.assertIsNone(result.get('sent'))
        self.assertTrue(result.get('attempted'))
        self.assertEqual(buffer.get_text(
            buffer.get_start_iter(), buffer.get_end_iter(), True),
            app.paste_chunks(message)[0])

    def test_delayed_paste_reads_dictation_even_after_shortcut_exits(self):
        class SuccessfulShortcut:
            def wait_finish(self, _result):
                return True

            def get_successful(self):
                return True

        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        old_url = 'https://example.test/copied-link'
        dictated = 'Please check my downloads for the audio file.'

        for request_delay in (100, 3000):
            with self.subTest(request_delay=request_delay):
                clipboard.set_text(old_url, -1)
                buffer = Gtk.TextBuffer()
                dictation = app.Dictation.__new__(app.Dictation)
                dictation.session = object()
                dictation.clipboard = clipboard
                dictation.clipboard_snapshot = {
                    'session': dictation.session, 'ready': True, 'text': old_url}
                dictation.paste_transaction = None
                result = {}
                loop = GLib.MainLoop()

                def read_clipboard():
                    buffer.paste_clipboard(clipboard, None, True)
                    return False

                def complete_shortcut(transaction):
                    dictation._paste_process_done(SuccessfulShortcut(), None, transaction)
                    return False

                def verified(transaction, ready):
                    result['verified'] = ready
                    transaction['attempted'] = True
                    GLib.timeout_add(request_delay, read_clipboard)
                    GLib.timeout_add(30, complete_shortcut, transaction)
                    return False

                dictation._paste_clipboard_verified = verified
                dictation._paste_target(
                    dictation.session, dictated,
                    lambda sent, _seconds, attempted: result.update(
                        sent=sent, attempted=attempted))
                GLib.timeout_add(request_delay + 250, lambda: (loop.quit(), False)[1])
                loop.run()

                pasted = buffer.get_text(
                    buffer.get_start_iter(), buffer.get_end_iter(), True)
                self.assertTrue(result.get('verified'))
                self.assertTrue(result.get('sent'))
                self.assertTrue(result.get('attempted'))
                self.assertEqual(pasted, dictated)
                self.assertEqual(clipboard.wait_for_text(), dictated)

    def test_aborted_paste_restores_previous_clipboard(self):
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        old_url = 'https://example.test/copied-link'
        dictated = 'Please check my downloads for the audio file.'
        clipboard.set_text(old_url, -1)
        dictation = app.Dictation.__new__(app.Dictation)
        dictation.session = object()
        dictation.clipboard = clipboard
        dictation.clipboard_snapshot = {
            'session': dictation.session, 'ready': True, 'text': old_url}
        dictation.paste_transaction = None
        result = {}
        loop = GLib.MainLoop()

        def verified(transaction, _ready):
            return app.Dictation._paste_clipboard_verified(dictation, transaction, False)

        dictation._paste_clipboard_verified = verified
        dictation._paste_target(
            dictation.session, dictated,
            lambda sent, _seconds, attempted: result.update(
                sent=sent, attempted=attempted))
        GLib.timeout_add(300, lambda: (loop.quit(), False)[1])
        loop.run()
        self.assertFalse(result.get('sent'))
        self.assertFalse(result.get('attempted'))
        self.assertEqual(clipboard.wait_for_text(), old_url)


if __name__ == '__main__':
    unittest.main()
