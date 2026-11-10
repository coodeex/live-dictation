"""Exercise delayed X11 clipboard reads on an isolated test display."""
import os
import shutil
import sys
import unittest

os.environ['LIVE_DICTATION_INSTANCE'] = 'dev'
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app
from gi.repository import Gdk, GLib, Gtk


@unittest.skipUnless(
    os.environ.get('LIVE_DICTATION_ISOLATED_CLIPBOARD_TEST') == '1' and
    shutil.which('xclip') is not None and
    Gdk.Display.get_default() is not None and
    Gdk.Display.get_default().get_name().startswith(':'),
    'requires an explicitly selected isolated X11 display and xclip')
class ClipboardPasteTests(unittest.TestCase):
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
