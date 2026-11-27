"""Enter button state and spoken-cue precedence, without microphone input."""
import os
import sys
import unittest
from types import SimpleNamespace

os.environ['LIVE_DICTATION_INSTANCE'] = 'dev'
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app


class StyleContext:
    def __init__(self):
        self.classes = set()

    def add_class(self, name):
        self.classes.add(name)

    def remove_class(self, name):
        self.classes.discard(name)


class Button:
    def __init__(self):
        self.context = StyleContext()
        self.tooltip = None

    def get_style_context(self):
        return self.context

    def set_tooltip_text(self, text):
        self.tooltip = text


class EnterModeTests(unittest.TestCase):
    def make_dictation(self, mode='on'):
        dictation = app.Dictation.__new__(app.Dictation)
        dictation.enter_mode = mode
        dictation.enter_mode_revision = 0
        dictation.enter_button = Button()
        dictation.prefs = {'press_enter': mode != 'off'}
        dictation._save_prefs = lambda: None
        dictation._refresh_enter_button()
        return dictation

    def test_button_cycles_on_once_off_on(self):
        dictation = self.make_dictation()
        self.assertIn('ld-enter-on', dictation.enter_button.context.classes)
        dictation._enter_changed(None)
        self.assertEqual(dictation.enter_mode, 'once')
        self.assertTrue(dictation.prefs['press_enter'])
        self.assertIn('ld-enter-once', dictation.enter_button.context.classes)
        self.assertNotIn('ld-enter-on', dictation.enter_button.context.classes)
        dictation._enter_changed(None)
        self.assertEqual(dictation.enter_mode, 'off')
        self.assertFalse(dictation.prefs['press_enter'])
        self.assertNotIn('ld-enter-once', dictation.enter_button.context.classes)
        dictation._enter_changed(None)
        self.assertEqual(dictation.enter_mode, 'on')
        self.assertTrue(dictation.prefs['press_enter'])
        self.assertEqual(dictation.enter_mode_revision, 3)

    def test_one_shot_returns_to_on_after_message_but_not_after_cancel(self):
        dictation = self.make_dictation('once')
        session = SimpleNamespace(enter_mode='once', enter_mode_revision=0)
        dictation._restore_enter_after_one_shot(session, 'cancelled', {'message': 'draft'})
        self.assertEqual(dictation.enter_mode, 'once')
        dictation._restore_enter_after_one_shot(session, 'complete', {'message': ''})
        self.assertEqual(dictation.enter_mode, 'once')
        dictation._restore_enter_after_one_shot(session, 'complete', {'message': 'draft'})
        self.assertEqual(dictation.enter_mode, 'on')
        self.assertIn('ld-enter-on', dictation.enter_button.context.classes)

    def test_manual_change_during_processing_is_not_overwritten(self):
        dictation = self.make_dictation('once')
        session = SimpleNamespace(enter_mode='once', enter_mode_revision=0)
        dictation._enter_changed(None)  # User selects persistent Off.
        dictation._restore_enter_after_one_shot(session, 'complete', {'message': 'draft'})
        self.assertEqual(dictation.enter_mode, 'off')

    def test_one_shot_overrides_spoken_send_but_off_still_allows_it(self):
        self.assertTrue(app.should_press_enter('on', None))
        self.assertFalse(app.should_press_enter('on', 'hold'))
        self.assertFalse(app.should_press_enter('off', None))
        self.assertTrue(app.should_press_enter('off', 'send'))
        self.assertFalse(app.should_press_enter('once', None))
        self.assertFalse(app.should_press_enter('once', 'send'))
        self.assertFalse(app.should_press_enter('on', 'send', format_requested=True))
        self.assertFalse(app.should_press_enter('on', None, join_uncertain=True))

    def test_delivery_presses_enter_only_for_allowed_states(self):
        for enter_mode, cue, should_send in (
                ('on', None, True), ('on', 'hold', False),
                ('once', None, False), ('once', 'send', False),
                ('off', None, False), ('off', 'send', True)):
            with self.subTest(enter_mode=enter_mode, cue=cue):
                dictation = app.Dictation.__new__(app.Dictation)
                dictation.session = SimpleNamespace(enter_mode=enter_mode)
                dictation.live = SimpleNamespace(set_text=lambda _text: None)
                dictation.status = SimpleNamespace(set_text=lambda _text: None)
                dictation.mode = 'cursor'
                dictation.caret_insertion = 'paste'
                dictation.delivery_error = False
                dictation._target_focused = lambda: True
                dictation._paste_target = lambda _session, _message, callback: callback(
                    True, .1, True)
                pressed = []
                dictation._press_enter = lambda: (pressed.append(True), True)[1]
                plan = {'message': 'Draft text.', 'cue': cue,
                        'format_requested': False, 'format_error': None,
                        'grammar_error': None, 'join_uncertain': False}
                completed = []
                dictation._text_event(dictation.session, plan, completed.append)
                self.assertEqual(bool(pressed), should_send)
                self.assertEqual(completed[0]['delivery_method'], 'paste')


if __name__ == '__main__':
    unittest.main()
