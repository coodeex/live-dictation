#!/usr/bin/env python3
"""Azure live dictation, with a GTK buffer and durable per-session history."""
import asyncio
import audioop
import datetime as dt
import json
import math
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import unicodedata
import wave

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Gio', '2.0')
gi.require_version('Atspi', '2.0')
gi.require_version('Pango', '1.0')
from gi.repository import Atspi, Gdk, Gio, GLib, Gtk, Pango

INSTANCE = os.environ.get('LIVE_DICTATION_INSTANCE', 'production')
if INSTANCE not in ('production', 'dev'):
    raise RuntimeError('LIVE_DICTATION_INSTANCE must be production or dev')
IS_DEV = INSTANCE == 'dev'
ROOT = pathlib.Path.home() / ('.local/share/live-dictation-dev' if IS_DEV else
                              '.local/share/live-dictation')
HISTORY = ROOT / 'history'
SETTINGS = ROOT / 'settings.json'
DRAFT = ROOT / 'buffer-draft.txt'
TIMINGS = ROOT / 'timings.jsonl'
STYLE = pathlib.Path(__file__).with_name('style.css')
SOCKET = pathlib.Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / (
    'live-dictation-dev.sock' if IS_DEV else 'live-dictation.sock')
CONFIG_HOME = pathlib.Path(os.environ.get(
    'XDG_CONFIG_HOME', pathlib.Path.home() / '.config')).expanduser()
CONFIG_PATH = pathlib.Path(os.environ.get(
    'LIVE_DICTATION_CONFIG', CONFIG_HOME / 'live-dictation/config.json')).expanduser()
BRIDGE = 'org.gnome.Shell.Extensions.LiveDictation'
BRIDGE_PATH = '/org/gnome/Shell/Extensions/LiveDictation'
HISTORY_PAGE_SIZE = 10
COMPACT_WINDOW_SIZE = (534, 334)
HISTORY_WINDOW_SIZE = (620, 700)
WORD_CORRECTIONS = {'Klarvo': 'Clarvo'}
FORMAT_PROMPT = ('Lead with the speaker\'s main point in the first sentence, using their own '
                 'wording when possible. Keep a useful topic sentence, but cut conversational '
                 'openers and filler. Examples of direct openings: "Could we move the meeting '
                 'to Thursday?"; "I think we should simplify the signup flow."; "The new '
                 'version is ready for review."; "The latest update is causing the app to '
                 'freeze."; "I\'d like to go with the second option." These are style examples '
                 'only; do not copy their facts into the text. '
                 'Fix grammar, repetition, and capitalization, including the '
                 'pronoun I. Make the text clearer and shorter while keeping the speaker\'s '
                 'words and meaning. Do not use em dashes; use commas or periods instead. '
                 'Add no facts or structure. Output only the revised text.')
PASTE_RESTORE_DELAY_MS = 1200
PASTE_VERIFY_TIMEOUT_SECONDS = 1.5
PASTE_KEYS = {
    'ctrl+v': ('29:1', '47:1', '47:0', '29:0'),
    'ctrl+shift+v': ('29:1', '42:1', '47:1', '47:0', '42:0', '29:0'),
}


def load_config():
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise RuntimeError(f'Cannot read {CONFIG_PATH}: {error}') from error
    if not isinstance(config, dict):
        raise RuntimeError(f'{CONFIG_PATH} must contain a JSON object')
    return config


CONFIG = load_config()
AZURE_ENDPOINT = (os.environ.get('AZURE_OPENAI_ENDPOINT') or
                  CONFIG.get('azure_endpoint') or '').rstrip('/')
DEPLOYMENT = (os.environ.get('AZURE_OPENAI_DEPLOYMENT') or
              CONFIG.get('deployment') or 'gpt-transcribe')
API_VERSION = (os.environ.get('AZURE_OPENAI_API_VERSION') or
               CONFIG.get('api_version') or '2025-03-01-preview')
FORMAT_DEPLOYMENT = (os.environ.get('AZURE_OPENAI_FORMAT_DEPLOYMENT') or
                     CONFIG.get('format_deployment') or '')
FORMAT_API_VERSION = (os.environ.get('AZURE_OPENAI_FORMAT_API_VERSION') or
                      CONFIG.get('format_api_version') or API_VERSION)
KEYMAP = str.maketrans(CONFIG.get('ydotool_keymap', {}))
PASTE_SHORTCUT = str(CONFIG.get('paste_shortcut', 'ctrl+v')).lower()
if PASTE_SHORTCUT not in PASTE_KEYS:
    raise RuntimeError('paste_shortcut must be ctrl+v or ctrl+shift+v')


def api_key():
    value = os.environ.get('AZURE_OPENAI_API_KEY', '').strip()
    if value:
        return value
    key_file = os.environ.get('AZURE_OPENAI_KEY_FILE') or CONFIG.get('api_key_file')
    if not key_file:
        raise RuntimeError('Set AZURE_OPENAI_API_KEY or api_key_file in the user config')
    path = pathlib.Path(os.path.expandvars(key_file)).expanduser()
    value = None
    try:
        with path.open() as source:
            for line in source:
                line = line.strip()
                if line.startswith('#'):
                    continue
                name, sep, raw = line.partition('=')
                if sep and name.strip() == 'AZURE_OPENAI_API_KEY':
                    value = raw.strip().strip('"').strip("'")
    except OSError as error:
        raise RuntimeError(f'Cannot read Azure key file {path}: {error}') from error
    if not value:
        raise RuntimeError(f'AZURE_OPENAI_API_KEY is missing from {path}')
    return value


def bridge(method):
    if IS_DEV:
        return None  # Never control production's GNOME focus helper from the dev instance.
    try:
        result = subprocess.run(['gdbus', 'call', '--session', '--dest', BRIDGE,
                                 '--object-path', BRIDGE_PATH,
                                 '--method', BRIDGE + '.' + method],
                                capture_output=True, text=True, timeout=1.5)
        if result.returncode != 0:
            return None
        return 'true' in result.stdout.lower()
    except (OSError, subprocess.TimeoutExpired):
        return None


def focused_accessible():
    """Find the deepest currently focused accessible without reading its content."""
    try:
        pending = [(Atspi.get_desktop(0), 0)]
        selected = None
        inspected = 0
        while pending and inspected < 4000:
            accessible, depth = pending.pop()
            inspected += 1
            try:
                if accessible.get_state_set().contains(Atspi.StateType.FOCUSED):
                    if selected is None or depth > selected[1]:
                        selected = (accessible, depth)
                count = min(accessible.get_child_count(), 500)
                for index in range(count - 1, -1, -1):
                    pending.append((accessible.get_child_at_index(index), depth + 1))
            except Exception:
                pass
        return selected[0] if selected else None
    except Exception:
        return None


def atomic_text(path, contents):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(contents)
    temp.replace(path)


def split_terminal_cue(transcript, allow_hold=False):
    """Return delivered text and its final spoken command, if any."""
    ending = transcript.rstrip()
    while ending and unicodedata.category(ending[-1])[0] in ('P', 'S'):
        ending = ending[:-1].rstrip()
    match = re.search(r'(?i)(?<!\w)(yolo|boom|hold|format)$', ending)
    if match is None:
        return transcript, None
    cue = match.group(1).lower()
    if cue == 'hold' and not allow_hold:
        return transcript, None
    message = ending[:match.start()].rstrip(' \t\r\n,;:([{\"\'“‘')
    if not message and cue not in ('hold', 'format'):
        return transcript, None
    return message, cue if cue in ('hold', 'format') else 'send'


def normalize_letter_plus(text):
    """Treat a plus between two ASCII letters as a word separator."""
    return re.sub(r'(?<=[A-Za-z])\+(?=[A-Za-z])', ' ', text)


def normalize_transcript_for_display(text):
    """Apply small, explicit corrections without changing the raw history."""
    text = normalize_letter_plus(text)
    for heard, preferred in WORD_CORRECTIONS.items():
        text = re.sub(rf'(?<!\w){re.escape(heard)}(?!\w)', lambda _match: preferred, text,
                      flags=re.IGNORECASE)
    return text


def delivery_plan(transcript, allow_hold=False, format_enabled=False):
    message, cue = split_terminal_cue(
        normalize_transcript_for_display(transcript), allow_hold=allow_hold)
    return {'message': message, 'cue': cue,
            'format_requested': bool(format_enabled or cue == 'format'),
            'format_error': None}


def append_timing(session, recording_seconds, processing_seconds, status,
                  transcription_seconds=None, azure_seconds=None,
                  format_seconds=None, format_error=False,
                  delivery_seconds=None, atspi_seconds=None,
                  ydotool_seconds=None, paste_seconds=None,
                  delivery_method=None, delivery_error=False):
    entry = {
        'session': session.path.name,
        'started': session.started,
        'status': status,
        'recording_seconds': round(recording_seconds, 3),
        'processing_seconds': (round(processing_seconds, 3)
                               if processing_seconds is not None else None),
        'transcription_seconds': (round(transcription_seconds, 3)
                                  if transcription_seconds is not None else None),
        'azure_seconds': (round(azure_seconds, 3) if azure_seconds is not None else None),
        'format_seconds': (round(format_seconds, 3)
                           if format_seconds is not None else None),
        'format_requested': session.format_requested,
        'format_error': format_error,
        'delivery_seconds': (round(delivery_seconds, 3)
                             if delivery_seconds is not None else None),
        'atspi_seconds': (round(atspi_seconds, 3) if atspi_seconds is not None else None),
        'ydotool_seconds': (round(ydotool_seconds, 3)
                            if ydotool_seconds is not None else None),
        'paste_seconds': (round(paste_seconds, 3)
                          if paste_seconds is not None else None),
        'delivery_method': delivery_method,
        'delivery_error': delivery_error,
    }
    descriptor = os.open(TIMINGS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, 'a', encoding='utf-8') as log:
        log.write(json.dumps(entry, ensure_ascii=False) + '\n')
    os.chmod(TIMINGS, 0o600)


def history_page(page, root=HISTORY, timings_path=TIMINGS):
    """Load one page of finished transcripts, newest first."""
    sessions = sorted((path for path in root.iterdir() if path.is_dir() and
                       (path / 'transcript.txt').is_file() and
                       (path / 'transcript.txt').stat().st_size > 1),
                      key=lambda path: path.name, reverse=True)
    total_pages = max(1, (len(sessions) + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    timings = {}
    try:
        with timings_path.open(encoding='utf-8') as source:
            for line in source:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get('session'), str):
                    timings[entry['session']] = entry
    except OSError:
        pass
    items = []
    for path in sessions[page * HISTORY_PAGE_SIZE:(page + 1) * HISTORY_PAGE_SIZE]:
        try:
            displayed = path / 'formatted.txt'
            if not displayed.is_file():
                displayed = path / 'transcript.txt'
            transcript = normalize_transcript_for_display(
                displayed.read_text(encoding='utf-8').rstrip('\n'))
            metadata = json.loads((path / 'metadata.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        timing = timings.get(path.name, {})
        recording = timing.get('recording_seconds')
        if not isinstance(recording, (int, float)):
            try:
                with wave.open(str(path / 'audio.wav'), 'rb') as audio:
                    recording = audio.getnframes() / audio.getframerate()
            except (OSError, EOFError, wave.Error, ZeroDivisionError):
                recording = None
        started = metadata.get('started') if isinstance(metadata, dict) else None
        try:
            stamp = dt.datetime.fromisoformat(started).astimezone()
        except (TypeError, ValueError):
            stamp = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        items.append({'transcript': transcript, 'started': stamp,
                      'recording_seconds': recording,
                      'processing_seconds': timing.get('processing_seconds'),
                      'transcription_seconds': timing.get('transcription_seconds'),
                      'delivery_seconds': timing.get('delivery_seconds'),
                      'delivery_method': timing.get('delivery_method')})
    return items, page, total_pages


class Session:
    def __init__(self, mode):
        stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
        self.path = HISTORY / f'{stamp}-{uuid.uuid4().hex[:8]}'
        self.path.mkdir(mode=0o700, parents=True)
        self.started = dt.datetime.now().astimezone().isoformat()
        self.mode = mode
        self.events = (self.path / 'events.jsonl').open('a', buffering=1)
        self.audio = wave.open(str(self.path / 'audio.wav'), 'wb')
        self.audio.setnchannels(1)
        self.audio.setsampwidth(2)
        self.audio.setframerate(24000)
        self.final_text = ''
        self.format_requested = False
        self.allow_hold = False
        self.audio_closed = False
        self.status = 'recording'
        self.write_metadata()

    def write_metadata(self):
        atomic_text(self.path / 'metadata.json', json.dumps({
            'started': self.started, 'ended': getattr(self, 'ended', None),
            'model': DEPLOYMENT, 'endpoint': AZURE_ENDPOINT, 'status': self.status,
            'initial_mode': self.mode, 'format_requested': self.format_requested},
                    indent=2) + '\n')

    def event(self, kind, **fields):
        self.events.write(json.dumps({'time': dt.datetime.now().astimezone().isoformat(),
                                      'kind': kind, **fields}, ensure_ascii=False) + '\n')

    def set_transcript(self, text):
        self.final_text = text
        self.event('final', text=text)
        self.save_transcript()

    def set_formatted_text(self, text):
        atomic_text(self.path / 'formatted.txt', text + '\n')
        self.event('formatted', chars=len(text))

    def transcript(self):
        return self.final_text

    def save_transcript(self):
        atomic_text(self.path / 'transcript.txt', self.transcript() + '\n')

    def close_audio(self):
        if not self.audio_closed:
            self.audio.close()
            self.audio_closed = True

    def finish(self, status):
        self.status = status
        self.ended = dt.datetime.now().astimezone().isoformat()
        self.close_audio()
        self.save_transcript()
        self.write_metadata()
        self.events.close()


class Dictation:
    def __init__(self):
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        HISTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ROOT, 0o700)
        os.chmod(HISTORY, 0o700)
        TIMINGS.touch(mode=0o600, exist_ok=True)
        os.chmod(TIMINGS, 0o600)
        self.prefs = {'mode': 'buffer' if IS_DEV else 'cursor',
                      'press_enter': False, 'pause_music': False,
                      'format': False,
                      'caret_insertion': 'paste'}
        if SETTINGS.exists():
            try:
                self.prefs.update(json.loads(SETTINGS.read_text()))
            except (ValueError, OSError):
                pass
        self.mode = self.prefs.get('mode', 'cursor')
        self.caret_insertion = self.prefs.get('caret_insertion', 'paste')
        if self.caret_insertion not in ('paste', 'type'):
            self.caret_insertion = 'paste'
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.clipboard_snapshot = None
        self.paste_transaction = None
        self.session = None
        self.stop_event = None
        self.cancel_event = None
        self.worker = None
        self.audio_loop = None
        self.audio_proc = None
        self.target_accessible = None
        self.wave_level = 0.0
        self.wave_display = 0.0
        self.wave_phase = 0.0
        self.wave_active = False
        self.wave_processing = False
        self.wave_timer = None
        self.voice_frames = 0
        self.mic_warning_timer = None
        self.mic_warning_dialog = None
        self.timing_timer = None
        self.close_timer = None
        self.record_started = None
        self.record_seconds = 0.0
        self.processing_started = None
        self.processing_seconds = None
        self.transcription_seconds = None
        self.delivery_started = None
        self.delivery_seconds = None
        self.paused_players = {}
        self.history_page_number = 0
        self._install_css()
        self._build_ui()
        self._socket_setup()

    def _install_css(self):
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(STYLE.read_text(encoding='utf-8').encode('utf-8'))
        except GLib.Error as error:
            print(f'CSS error: {error}', file=sys.stderr)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.css_provider = provider

    def _build_ui(self):
        title_text = 'Live dictation · DEV' if IS_DEV else 'Live dictation'
        w = self.window = Gtk.Window(title=title_text)
        wmclass = 'live-dictation-dev' if IS_DEV else 'live-dictation'
        w.set_wmclass(wmclass, wmclass)
        w.set_default_size(*COMPACT_WINDOW_SIZE)
        w.set_size_request(*COMPACT_WINDOW_SIZE)
        w.set_resizable(False)
        w.set_keep_above(True)
        w.set_decorated(False)
        w.set_app_paintable(True)
        w.get_style_context().add_class('ld-window')
        screen = w.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            w.set_visual(visual)
        else:
            w.get_style_context().add_class('ld-opaque')
        w.set_focus_on_map(False)
        w.set_accept_focus(self.mode == 'buffer')
        w.connect('delete-event', self._close_popup)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.get_style_context().add_class('ld-card')
        w.add(card)
        self.view_stack = Gtk.Stack()
        self.view_stack.set_hhomogeneous(False)
        self.view_stack.set_vhomogeneous(False)
        card.pack_start(self.view_stack, True, True, 0)
        dictation_view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.view_stack.add_named(dictation_view, 'dictation')

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        dictation_view.pack_start(header, False, False, 0)
        drag_strip = Gtk.EventBox()
        drag_strip.set_visible_window(True)
        drag_strip.set_size_request(-1, 10)
        drag_strip.get_style_context().add_class('ld-drag-area')
        drag_strip.connect('button-press-event', self._drag)
        drag_strip.connect('realize', self._set_drag_cursor)
        header.pack_start(drag_strip, False, False, 0)
        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.pack_start(header_row, False, False, 0)
        self.recording_dot = Gtk.Box()
        self.recording_dot.set_size_request(8, 8)
        self.recording_dot.set_valign(Gtk.Align.CENTER)
        self.recording_dot.get_style_context().add_class('ld-dot')
        header_row.pack_start(self.recording_dot, False, False, 0)
        title_handle = Gtk.EventBox()
        title_handle.set_visible_window(True)
        title_handle.get_style_context().add_class('ld-drag-area')
        title_handle.connect('button-press-event', self._drag)
        title_handle.connect('realize', self._set_drag_cursor)
        header_row.pack_start(title_handle, True, True, 0)
        title_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title_handle.add(title_stack)
        title = Gtk.Label(label=title_text)
        title.set_xalign(0)
        title.get_style_context().add_class('ld-title')
        title_stack.pack_start(title, False, False, 0)
        self.status = Gtk.Label(label='Ready · F4 to start')
        self.status.set_xalign(0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.get_style_context().add_class('ld-status')
        title_stack.pack_start(self.status, False, False, 0)

        modes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        modes.get_style_context().add_class('ld-seg')
        header_row.pack_start(modes, False, False, 0)
        self.cursor_button = Gtk.RadioButton.new_with_label_from_widget(None, 'Caret')
        self.buffer_button = Gtk.RadioButton.new_with_label_from_widget(self.cursor_button, 'Buffer')
        for button, edge in ((self.cursor_button, 'ld-seg-first'),
                             (self.buffer_button, 'ld-seg-last')):
            button.set_mode(False)
            button.get_style_context().add_class('ld-seg-btn')
            button.get_style_context().add_class(edge)
        modes.pack_start(self.cursor_button, False, False, 0)
        modes.pack_start(self.buffer_button, False, False, 0)
        (self.cursor_button if self.mode == 'cursor' else self.buffer_button).set_active(True)
        self.cursor_button.connect('toggled', self._mode_changed)
        self.buffer_button.connect('toggled', self._mode_changed)
        self.format_button = Gtk.ToggleButton(label='✦')
        self.format_button.set_relief(Gtk.ReliefStyle.NONE)
        self.format_button.set_can_focus(False)
        self.format_button.set_size_request(24, 24)
        self.format_button.set_active(self.prefs.get('format') is True)
        self.format_button.set_tooltip_text('Format dictated text with AI · never press Enter')
        self.format_button.get_style_context().add_class('ld-format-toggle')
        self.format_button.connect('toggled', self._format_changed)
        header_row.pack_start(self.format_button, False, False, 0)
        self.enter_button = Gtk.ToggleButton(label='↵')
        self.enter_button.set_relief(Gtk.ReliefStyle.NONE)
        self.enter_button.set_can_focus(False)
        self.enter_button.set_size_request(24, 24)
        self.enter_button.set_active(self.prefs.get('press_enter') is True)
        self.enter_button.set_sensitive(self.mode == 'cursor')
        self.enter_button.set_tooltip_text('Press Enter after inserting at caret')
        self.enter_button.get_style_context().add_class('ld-enter-toggle')
        self.enter_button.connect('toggled', self._enter_changed)
        header_row.pack_start(self.enter_button, False, False, 0)
        self.music_button = Gtk.ToggleButton(label='♫')
        self.music_button.set_relief(Gtk.ReliefStyle.NONE)
        self.music_button.set_can_focus(False)
        self.music_button.set_size_request(24, 24)
        self.music_button.set_active(self.prefs.get('pause_music') is True)
        self.music_button.set_tooltip_text('Pause playing media during dictation')
        self.music_button.get_style_context().add_class('ld-music-toggle')
        self.music_button.connect('toggled', self._music_changed)
        header_row.pack_start(self.music_button, False, False, 0)
        close = Gtk.Button(label='×')
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_can_focus(False)
        close.set_size_request(22, 22)
        close.set_tooltip_text('Close popup')
        close.get_style_context().add_class('ld-close')
        close.connect('clicked', self._close_popup)
        header_row.pack_start(close, False, False, 0)

        self.live = Gtk.Label(label='')
        self.live.set_xalign(0)
        self.live.set_line_wrap(True)
        self.live.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.live.set_ellipsize(Pango.EllipsizeMode.END)
        self.live.set_lines(3)
        self.live.set_max_width_chars(58)
        self.live.set_size_request(-1, 36)
        self.live.get_style_context().add_class('ld-live')
        live_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        dictation_view.pack_start(live_row, False, False, 0)
        self.timing = Gtk.Label(label='0:00')
        self.timing.set_xalign(0)
        self.timing.set_width_chars(20)
        self.timing.set_tooltip_text('Recording · transcription · delivery')
        self.timing.get_style_context().add_class('ld-timing')
        live_row.pack_start(self.timing, False, False, 0)
        live_row.pack_start(self.live, True, True, 0)
        wave_frame = Gtk.Box()
        wave_frame.get_style_context().add_class('ld-waveform-frame')
        live_row.pack_end(wave_frame, False, False, 0)
        self.waveform = Gtk.DrawingArea()
        self.waveform.set_size_request(96, 30)
        self.waveform.connect('draw', self._draw_waveform)
        wave_frame.add(self.waveform)
        self.wave_frame = wave_frame
        editor_overlay = Gtk.Overlay()
        dictation_view.pack_start(editor_overlay, True, True, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_size_request(-1, 95)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_overlay_scrolling(True)
        scroll.set_shadow_type(Gtk.ShadowType.NONE)
        scroll.get_style_context().add_class('ld-editor-frame')
        editor_overlay.add(scroll)
        self.text = Gtk.TextView()
        self.text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text.set_left_margin(10)
        self.text.set_right_margin(78)
        self.text.set_top_margin(8)
        self.text.set_bottom_margin(8)
        self.text.get_style_context().add_class('ld-editor')
        if DRAFT.exists():
            self.text.get_buffer().set_text(DRAFT.read_text())
        self.text.get_buffer().connect('changed', self._buffer_changed)
        scroll.add(self.text)
        self.copy_icon = Gtk.Image.new_from_icon_name('edit-copy-symbolic', Gtk.IconSize.MENU)
        self.copy_button = Gtk.Button(image=self.copy_icon)
        self.copy_button.set_relief(Gtk.ReliefStyle.NONE)
        self.copy_button.set_can_focus(False)
        self.copy_button.set_size_request(28, 28)
        self.copy_button.set_tooltip_text('Copy buffer, clear it, and close')
        self.copy_button.get_style_context().add_class('ld-copy-icon')
        self.copy_button.connect('clicked', self._copy)
        self.clear_button = Gtk.Button(
            image=Gtk.Image.new_from_icon_name('edit-clear-symbolic', Gtk.IconSize.MENU)
        )
        self.clear_button.set_relief(Gtk.ReliefStyle.NONE)
        self.clear_button.set_can_focus(False)
        self.clear_button.set_size_request(28, 28)
        self.clear_button.set_tooltip_text('Clear buffer')
        self.clear_button.get_style_context().add_class('ld-copy-icon')
        self.clear_button.connect('clicked', self._confirm_clear_buffer)
        editor_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        editor_actions.set_halign(Gtk.Align.END)
        editor_actions.set_valign(Gtk.Align.START)
        editor_actions.set_margin_top(8)
        editor_actions.set_margin_end(8)
        editor_actions.pack_start(self.clear_button, False, False, 0)
        editor_actions.pack_start(self.copy_button, False, False, 0)
        editor_overlay.add_overlay(editor_actions)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dictation_view.pack_start(buttons, False, False, 0)
        self.cancel_button = Gtk.Button(label='Cancel')
        self.cancel_button.set_no_show_all(True)
        self.cancel_button.get_style_context().add_class('ld-ghost')
        self.cancel_button.connect('clicked', self._cancel_recording)
        buttons.pack_start(self.cancel_button, False, False, 0)
        buttons.pack_start(Gtk.Box(), True, True, 0)
        self.history_button = Gtk.Button(label='History')
        self.history_button.connect('clicked', self._show_history)
        self.history_button.get_style_context().add_class('ld-ghost')
        buttons.pack_start(self.history_button, False, False, 0)
        self.toggle_button = Gtk.Button(label='Start · F4')
        self.toggle_button.connect('clicked', lambda *_: self.toggle())
        self.toggle_button.get_style_context().add_class('ld-primary')
        buttons.pack_start(self.toggle_button, False, False, 0)
        self._build_history_view()
        self.view_stack.set_visible_child_name('dictation')
        self.window.show_all()
        self.window.hide()

    def _build_history_view(self):
        view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.view_stack.add_named(view, 'history')

        drag_strip = Gtk.EventBox()
        drag_strip.set_visible_window(True)
        drag_strip.set_size_request(-1, 10)
        drag_strip.get_style_context().add_class('ld-drag-area')
        drag_strip.connect('button-press-event', self._drag)
        drag_strip.connect('realize', self._set_drag_cursor)
        view.pack_start(drag_strip, False, False, 0)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        view.pack_start(header, False, False, 0)
        back = Gtk.Button(label='← Back')
        back.get_style_context().add_class('ld-ghost')
        back.connect('clicked', self._back_from_history)
        header.pack_start(back, False, False, 0)
        title_handle = Gtk.EventBox()
        title_handle.set_visible_window(True)
        title_handle.get_style_context().add_class('ld-drag-area')
        title_handle.connect('button-press-event', self._drag)
        title_handle.connect('realize', self._set_drag_cursor)
        header.pack_start(title_handle, True, True, 0)
        title = Gtk.Label(label='History')
        title.set_xalign(0)
        title.get_style_context().add_class('ld-title')
        title_handle.add(title)
        close = Gtk.Button(label='×')
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_can_focus(False)
        close.set_size_request(22, 22)
        close.set_tooltip_text('Close popup')
        close.get_style_context().add_class('ld-close')
        close.connect('clicked', self._close_popup)
        header.pack_end(close, False, False, 0)

        self.history_scroller = Gtk.ScrolledWindow()
        self.history_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.history_scroller.set_overlay_scrolling(True)
        self.history_scroller.set_shadow_type(Gtk.ShadowType.NONE)
        self.history_scroller.get_style_context().add_class('ld-history-scroll')
        view.pack_start(self.history_scroller, True, True, 0)
        self.history_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.history_list.set_margin_end(4)
        viewport = Gtk.Viewport()
        viewport.set_shadow_type(Gtk.ShadowType.NONE)
        viewport.add(self.history_list)
        self.history_scroller.add(viewport)

        pager = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        view.pack_end(pager, False, False, 0)
        self.history_previous = Gtk.Button(label='← Newer')
        self.history_previous.get_style_context().add_class('ld-ghost')
        self.history_previous.connect('clicked', lambda *_: self._change_history_page(-1))
        pager.pack_start(self.history_previous, False, False, 0)
        self.history_page_label = Gtk.Label()
        self.history_page_label.get_style_context().add_class('ld-history-page')
        pager.pack_start(self.history_page_label, True, True, 0)
        self.history_next = Gtk.Button(label='Older →')
        self.history_next.get_style_context().add_class('ld-ghost')
        self.history_next.connect('clicked', lambda *_: self._change_history_page(1))
        pager.pack_end(self.history_next, False, False, 0)

    @staticmethod
    def _history_duration(seconds):
        if not isinstance(seconds, (int, float)) or seconds < 0:
            return '—'
        whole = int(seconds)
        return f'{whole // 60}:{whole % 60:02d}'

    def _history_card(self, item):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class('ld-history-card')
        heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        card.pack_start(heading, False, False, 0)
        date = item['started'].strftime('%d %b %Y · %H:%M')
        date_label = Gtk.Label(label=date)
        date_label.set_xalign(0)
        date_label.get_style_context().add_class('ld-history-date')
        heading.pack_start(date_label, True, True, 0)
        if item['transcription_seconds'] is not None:
            durations = (f"Recording {self._history_duration(item['recording_seconds'])}"
                         f"  ·  Transcribe {self._history_duration(item['transcription_seconds'])}"
                         f"  ·  Write {self._history_duration(item['delivery_seconds'])}")
        else:
            durations = (f"Recording {self._history_duration(item['recording_seconds'])}"
                         f"  ·  Processing {self._history_duration(item['processing_seconds'])}")
        duration_label = Gtk.Label(label=durations)
        duration_label.get_style_context().add_class('ld-history-duration')
        heading.pack_end(duration_label, False, False, 0)

        excerpt = ' '.join(item['transcript'].split())
        preview = Gtk.Label(label=excerpt[:100] + ('…' if len(excerpt) > 100 else ''))
        preview.set_xalign(0)
        preview.set_line_wrap(True)
        preview.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        preview.set_no_show_all(True)
        preview.get_style_context().add_class('ld-history-preview')
        card.pack_start(preview, False, False, 0)

        full = Gtk.ScrolledWindow()
        full.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        full.set_overlay_scrolling(True)
        full.set_shadow_type(Gtk.ShadowType.NONE)
        full.set_size_request(-1, 170)
        full.set_no_show_all(True)
        full.get_style_context().add_class('ld-history-full')
        full_text = Gtk.TextView()
        full_text.set_editable(False)
        full_text.set_cursor_visible(False)
        full_text.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        full_text.set_left_margin(10)
        full_text.set_right_margin(10)
        full_text.set_top_margin(8)
        full_text.set_bottom_margin(8)
        full_text.get_style_context().add_class('ld-history-text')
        full_text.get_buffer().set_text(item['transcript'])
        full.add(full_text)
        card.pack_start(full, False, False, 0)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        card.pack_start(actions, False, False, 0)
        actions.pack_start(Gtk.Box(), True, True, 0)
        copy = Gtk.Button(label='Copy')
        copy.get_style_context().add_class('ld-ghost')
        copy.connect('clicked', self._copy_history, item['transcript'])
        actions.pack_start(copy, False, False, 0)
        expand = Gtk.Button(label='Expand')
        expand.get_style_context().add_class('ld-ghost')
        def toggle_expansion(button):
            expanded = not full.get_visible()
            full.set_visible(expanded)
            preview.set_visible(not expanded)
            button.set_label('Collapse' if expanded else 'Expand')

        expand.connect('clicked', toggle_expansion)
        actions.pack_start(expand, False, False, 0)
        card.show_all()
        full_text.show()
        full.hide()
        preview.show()
        return card

    def _copy_history(self, button, transcript):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(transcript, -1)
        button.set_label('✓ Copied')
        GLib.timeout_add(2000, self._reset_history_copy, button)

    @staticmethod
    def _reset_history_copy(button):
        try:
            button.set_label('Copy')
        except RuntimeError:
            pass  # The card may have been destroyed by paging or Back.
        return False

    def _render_history_page(self):
        for child in self.history_list.get_children():
            child.destroy()
        try:
            items, self.history_page_number, total_pages = history_page(
                self.history_page_number)
        except OSError as error:
            items, total_pages = [], 1
            message = Gtk.Label(label=f'Could not load history: {error}')
            message.get_style_context().add_class('ld-history-empty')
            self.history_list.pack_start(message, False, False, 0)
        if not items and not self.history_list.get_children():
            empty = Gtk.Label(label='No saved transcriptions yet')
            empty.get_style_context().add_class('ld-history-empty')
            self.history_list.pack_start(empty, False, False, 0)
        for item in items:
            self.history_list.pack_start(self._history_card(item), False, False, 0)
        self.history_list.show_all()
        self.history_page_label.set_text(f'{self.history_page_number + 1} / {total_pages}')
        self.history_previous.set_sensitive(self.history_page_number > 0)
        self.history_next.set_sensitive(self.history_page_number + 1 < total_pages)
        self.history_scroller.get_vadjustment().set_value(0)

    def _change_history_page(self, delta):
        self.history_page_number += delta
        self._render_history_page()

    def _show_history(self, *_args):
        if self.stop_event is not None:
            return
        self._cancel_close_timer()
        if self.mode == 'cursor' and not self.window.has_toplevel_focus():
            current = focused_accessible()
            if current is not None:
                self.target_accessible = current
        self.history_page_number = 0
        self._render_history_page()
        self.view_stack.set_visible_child_name('history')
        self.window.set_accept_focus(True)
        self.window.set_size_request(*HISTORY_WINDOW_SIZE)
        self.window.resize(*HISTORY_WINDOW_SIZE)
        self.window.present()
        GLib.idle_add(self._reposition_top_right)
        GLib.timeout_add(100, self._reposition_top_right)
        self._ensure_topmost()

    def _back_from_history(self, *_args):
        if self.view_stack.get_visible_child_name() != 'history':
            return
        self.view_stack.set_visible_child_name('dictation')
        self.window.set_size_request(*COMPACT_WINDOW_SIZE)
        self.window.resize(*COMPACT_WINDOW_SIZE)
        self.window.set_accept_focus(self.mode == 'buffer')
        GLib.idle_add(self._reposition_top_right)
        GLib.timeout_add(100, self._reposition_top_right)
        if self.mode == 'cursor':
            self._restore_target()

    def _set_drag_cursor(self, widget):
        widget.get_window().set_cursor(Gdk.Cursor.new_from_name(widget.get_display(), 'move'))

    def _drag(self, _widget, event):
        if event.button != 1:
            return False
        self.window.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
        return True

    def _draw_waveform(self, widget, context):
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        bar_width, gap, count = 3.0, 3.0, 15
        left = (width - count * bar_width - (count - 1) * gap) / 2
        level = self.wave_display if self.wave_active else 0.0
        for index in range(count):
            distance = abs(index - (count - 1) / 2) / ((count - 1) / 2)
            shape = 0.35 + 0.65 * (1 - distance)
            pulse = 0.60 + 0.40 * math.sin(self.wave_phase + index * 0.83) ** 2
            if self.wave_processing:
                sweep = (count - 1) * (0.5 + 0.5 * math.sin(self.wave_phase))
                glow = math.exp(-((index - sweep) / 2.6) ** 2)
                bar_height = 3 + (height - 7) * glow * (0.62 + 0.38 * pulse)
            else:
                bar_height = 3 + level * (height - 5) * shape * pulse
            x = left + index * (bar_width + gap)
            y = (height - bar_height) / 2
            radius = bar_width / 2
            context.new_sub_path()
            context.arc(x + radius, y + radius, radius, math.pi, 2 * math.pi)
            context.arc(x + radius, y + bar_height - radius, radius, 0, math.pi)
            context.close_path()
            if self.wave_processing:
                context.set_source_rgba(0.431, 0.906, 0.718, 0.24 + 0.72 * glow)
            elif self.wave_active:
                context.set_source_rgba(0.204, 0.827, 0.600, 0.55 + 0.35 * level)
            else:
                context.set_source_rgba(1.0, 1.0, 1.0, 0.23)
            context.fill()
        return False

    def _set_wave_active(self, active):
        self.wave_active = active
        self.wave_level = 0.0
        self.wave_display = 0.0
        self.wave_phase = 0.0
        context = self.wave_frame.get_style_context()
        if active:
            context.add_class('ld-waveform-active')
            if self.wave_timer is None:
                self.wave_timer = GLib.timeout_add(45, self._animate_waveform)
        else:
            context.remove_class('ld-waveform-active')
            if self.wave_timer is not None:
                GLib.source_remove(self.wave_timer)
                self.wave_timer = None
        self.waveform.queue_draw()

    def _set_wave_processing(self, processing):
        self.wave_processing = processing
        context = self.wave_frame.get_style_context()
        dot = self.recording_dot.get_style_context()
        if processing:
            context.add_class('ld-waveform-processing')
            dot.add_class('ld-dot-processing')
            if self.wave_timer is None:
                self.wave_timer = GLib.timeout_add(45, self._animate_waveform)
        else:
            context.remove_class('ld-waveform-processing')
            dot.remove_class('ld-dot-processing')
            if not self.wave_active and self.wave_timer is not None:
                GLib.source_remove(self.wave_timer)
                self.wave_timer = None
        self.waveform.queue_draw()

    def _set_wave_level(self, session, level, rms):
        if self.session is session and self.wave_active:
            self.wave_level = level
            if rms >= 270:
                self.voice_frames += 1
            elif self.voice_frames < 3:
                self.voice_frames = 0
        return False

    def _check_initial_voice(self, session):
        self.mic_warning_timer = None
        if (self.session is not session or self.stop_event is None or
                self.stop_event.is_set() or self.voice_frames >= 3):
            return False
        self.status.set_text('No voice detected · check microphone')
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.CLOSE,
            text='Your microphone may not be hearing you',
        )
        dialog.format_secondary_text(
            'No voice was detected in the first 7 seconds. Check your microphone and input device.'
        )
        dialog.connect('response', self._close_mic_warning)
        self.mic_warning_dialog = dialog
        dialog.show_all()
        return False

    def _close_mic_warning(self, dialog, _response=None):
        if self.mic_warning_dialog is dialog:
            self.mic_warning_dialog = None
        dialog.destroy()
        if self.stop_event is not None and not self.stop_event.is_set() and self.mode == 'cursor':
            GLib.timeout_add(150, self._restore_after_show)

    def _dismiss_mic_warning(self):
        if self.mic_warning_timer is not None:
            GLib.source_remove(self.mic_warning_timer)
            self.mic_warning_timer = None
        if self.mic_warning_dialog is not None:
            dialog = self.mic_warning_dialog
            self.mic_warning_dialog = None
            dialog.destroy()

    def _animate_waveform(self):
        if not self.wave_active and not self.wave_processing:
            self.wave_timer = None
            return False
        self.wave_phase += 0.13 if self.wave_processing else 0.21
        if self.wave_active:
            self.wave_display += (max(0.18, self.wave_level) - self.wave_display) * 0.32
        self.waveform.queue_draw()
        return True

    @staticmethod
    def _clock(seconds):
        whole = max(0, int(seconds))
        return f'{whole // 60}:{whole % 60:02d}'

    def _update_timing(self):
        recording = (time.monotonic() - self.record_started
                     if self.record_started is not None and self.processing_started is None
                     else self.record_seconds)
        display = self._clock(recording)
        if self.processing_started is not None:
            if self.transcription_seconds is None:
                processing = (time.monotonic() - self.processing_started
                              if self.processing_seconds is None else self.processing_seconds)
                display += ' · ' + self._clock(processing)
            elif self.delivery_started is None:
                display += ' · ' + self._clock(self.transcription_seconds)
            else:
                delivery = (time.monotonic() - self.delivery_started
                            if self.delivery_seconds is None
                            else self.delivery_seconds or 0)
                display += (' · ' + self._clock(self.transcription_seconds) +
                            ' · ' + self._clock(delivery))
        self.timing.set_text(display)
        return True

    def _cancel_close_timer(self):
        if self.close_timer is not None:
            GLib.source_remove(self.close_timer)
            self.close_timer = None

    def _hide_completed(self):
        self.close_timer = None
        if self.stop_event is None and self.mode == 'cursor' and not self.delivery_error:
            self.window.hide()
        return False

    def _set_cancel_visible(self, visible):
        self.cancel_button.set_no_show_all(not visible)
        if visible:
            self.cancel_button.show()
        else:
            self.cancel_button.hide()

    def _socket_setup(self):
        if SOCKET.exists():
            try:
                with socket.socket(socket.AF_UNIX) as check:
                    check.settimeout(.2)
                    check.connect(str(SOCKET))
                raise RuntimeError('Live dictation is already running')
            except ConnectionRefusedError:
                SOCKET.unlink()
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(SOCKET))
        os.chmod(SOCKET, 0o600)
        self.listener.listen(4)
        self.listener.setblocking(False)
        GLib.io_add_watch(self.listener.fileno(), GLib.IO_IN, self._command)

    def _command(self, _source, _condition):
        try:
            connection, _ = self.listener.accept()
            with connection:
                command = connection.recv(64).decode().strip()
                if command == 'toggle':
                    self.toggle()
                elif command == 'show':
                    self._open_top_right()
                elif command == 'reassert-topmost':
                    self._ensure_topmost()
                elif command == 'buffer':
                    self.buffer_button.set_active(True)
                elif command == 'cursor':
                    self.cursor_button.set_active(True)
                elif command in ('paste', 'type'):
                    self.caret_insertion = command
                    self.prefs['caret_insertion'] = command
                    self._save_prefs()
                    self.status.set_text(f'Caret insertion: {command}')
                elif command == 'status':
                    gdk_window = self.window.get_window()
                    connection.sendall(json.dumps({'mode': self.mode,
                        'caret_insertion': self.caret_insertion,
                        'recording': self.stop_event is not None and not self.stop_event.is_set(),
                        'finalizing': self.stop_event is not None and self.stop_event.is_set(),
                        'microphone_running': self.audio_proc is not None and self.audio_proc.returncode is None,
                        'popup_visible': self.window.get_visible(),
                        'popup_focused': self.window.has_toplevel_focus(),
                        'popup_above': bool(gdk_window and
                            gdk_window.get_state() & Gdk.WindowState.ABOVE)}).encode())
        except OSError as error:
            self.status.set_text(f'Control error: {error}')
        return True

    def _close_popup(self, *_args):
        self._cancel_close_timer()
        if self.stop_event is not None and not self.stop_event.is_set():
            self._cancel_recording()
        self.window.hide()
        return True

    def _ensure_topmost(self):
        # The GTK property can remain true after hide/show even when the X11
        # _NET_WM_STATE_ABOVE request was lost. Reissue the actual WM request.
        self.window.set_keep_above(False)
        self.window.set_keep_above(True)

    def _move_window(self, x, y):
        self.window.move(x, y)
        return False

    def _top_right_position(self):
        display = self.window.get_display()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_workarea()
        width, height = self.window.get_size()
        return (max(area.x, area.x + area.width - width - 8), area.y + 8)

    def _reposition_top_right(self):
        self._move_window(*self._top_right_position())
        return False

    def _open_top_right(self):
        position = self._top_right_position()
        self._move_window(*position)
        self.window.show_all()
        self._move_window(*position)
        GLib.idle_add(self._move_window, *position)
        self._ensure_topmost()

    def _save_prefs(self):
        self.prefs['mode'] = self.mode
        atomic_text(SETTINGS, json.dumps(self.prefs, indent=2) + '\n')
        os.chmod(SETTINGS, 0o600)

    def _enter_changed(self, button):
        self.prefs['press_enter'] = button.get_active()
        self._save_prefs()

    def _format_changed(self, button):
        self.prefs['format'] = button.get_active()
        self._save_prefs()

    def _music_changed(self, button):
        self.prefs['pause_music'] = button.get_active()
        self._save_prefs()
        if self.stop_event is not None and not self.stop_event.is_set():
            if button.get_active():
                self._pause_music()
            else:
                self._resume_music()

    @staticmethod
    def _mpris_call(bus, destination, path, interface, method, parameters=None,
                    reply_type=None):
        return bus.call_sync(destination, path, interface, method, parameters,
                             reply_type, Gio.DBusCallFlags.NONE, 1000, None)

    def _mpris_owner(self, bus, name):
        reply = self._mpris_call(bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                                 'org.freedesktop.DBus', 'GetNameOwner',
                                 GLib.Variant('(s)', (name,)), GLib.VariantType('(s)'))
        return reply.unpack()[0]

    def _mpris_status(self, bus, name):
        reply = self._mpris_call(bus, name, '/org/mpris/MediaPlayer2',
                                 'org.freedesktop.DBus.Properties', 'Get',
                                 GLib.Variant('(ss)', ('org.mpris.MediaPlayer2.Player',
                                                        'PlaybackStatus')),
                                 GLib.VariantType('(v)'))
        return reply.get_child_value(0).get_variant().get_string()

    def _pause_music(self):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            names = self._mpris_call(bus, 'org.freedesktop.DBus',
                                      '/org/freedesktop/DBus', 'org.freedesktop.DBus',
                                      'ListNames', reply_type=GLib.VariantType('(as)'))
        except GLib.Error as error:
            print(f'Could not inspect media players: {error}', file=sys.stderr)
            return
        for name in names.unpack()[0]:
            if not name.startswith('org.mpris.MediaPlayer2.') or name in self.paused_players:
                continue
            try:
                owner = self._mpris_owner(bus, name)
                if self._mpris_status(bus, name) != 'Playing':
                    continue
                self._mpris_call(bus, name, '/org/mpris/MediaPlayer2',
                                 'org.mpris.MediaPlayer2.Player', 'Pause')
                self.paused_players[name] = owner
            except GLib.Error as error:
                print(f'Could not pause {name}: {error}', file=sys.stderr)

    def _resume_music(self):
        paused = self.paused_players
        self.paused_players = {}
        if not paused:
            return False
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as error:
            print(f'Could not reconnect to media players: {error}', file=sys.stderr)
            return False
        for name, owner in paused.items():
            try:
                if (self._mpris_owner(bus, name) == owner and
                        self._mpris_status(bus, name) == 'Paused'):
                    self._mpris_call(bus, name, '/org/mpris/MediaPlayer2',
                                     'org.mpris.MediaPlayer2.Player', 'Play')
            except GLib.Error as error:
                print(f'Could not resume {name}: {error}', file=sys.stderr)
        return False

    def _mode_changed(self, button):
        if not button.get_active():
            return
        self._cancel_close_timer()
        position = (self.window.get_position() if self.window.get_visible()
                    else self._top_right_position())
        self.mode = 'cursor' if button is self.cursor_button else 'buffer'
        self.enter_button.set_sensitive(self.mode == 'cursor')
        if self.session:
            self.session.event('destination', mode=self.mode)
        self._save_prefs()
        if self.mode == 'cursor':
            self.window.hide()
            self.window.set_accept_focus(False)
            self._move_window(*position)
            self.window.show_all()
            self._move_window(*position)
            GLib.idle_add(self._move_window, *position)
            self._ensure_topmost()
            restored = self._restore_target()
            if restored is False:
                self.status.set_text('Caret target unavailable · switch to Buffer')
        else:
            self.window.set_accept_focus(True)
            self.window.present()
            self._move_window(*position)
            GLib.idle_add(self._move_window, *position)

    def _copy(self, *_args):
        buffer = self.text.get_buffer()
        value = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(value, -1)
        buffer.set_text('')
        self.cursor_button.set_active(True)
        self._close_popup()

    def _confirm_clear_buffer(self, *_args):
        dialog = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            destroy_with_parent=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text='Clear buffer?',
        )
        dialog.format_secondary_text('Do you really want to clear the buffer?')
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        dialog.add_button('Clear buffer', Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            self.text.get_buffer().set_text('')
            self.status.set_text('Buffer cleared')

    def _buffer_changed(self, buffer):
        value = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        atomic_text(DRAFT, value)
        os.chmod(DRAFT, 0o600)

    def _restore_target(self):
        result = bridge('RestoreTarget')
        if result is not None:
            return result
        if self.target_accessible is not None:
            try:
                return bool(self.target_accessible.grab_focus())
            except Exception:
                return False
        return False

    def _target_focused(self):
        result = bridge('IsTargetFocused')
        if result is not None:
            return result
        if self.target_accessible is not None:
            try:
                return self.target_accessible.get_state_set().contains(Atspi.StateType.FOCUSED)
            except Exception:
                return False
        return not self.window.has_toplevel_focus()

    def _save_clipboard_for_paste(self, session):
        snapshot = {'session': session, 'ready': False, 'text': None}
        self.clipboard_snapshot = snapshot
        self.clipboard.request_text(self._clipboard_snapshot_received, snapshot)

    def _clipboard_snapshot_received(self, _clipboard, text, snapshot):
        if self.clipboard_snapshot is snapshot and self.session is snapshot['session']:
            snapshot['text'] = text
            snapshot['ready'] = True

    def _paste_target(self, session, value, done):
        snapshot = self.clipboard_snapshot
        saved_text = (snapshot['text'] if snapshot is not None and
                      snapshot['session'] is session and snapshot['ready'] else None)
        transaction = {'session': session, 'value': value, 'saved_text': saved_text,
                       'done': done, 'started': time.monotonic(),
                       'paste_seconds': None, 'sent': False, 'restore_timeout': None,
                       'command_timeout': None, 'process': None, 'timed_out': False,
                       'attempted': False}
        self.paste_transaction = transaction
        try:
            self.clipboard.set_text(value, -1)
        except Exception as error:
            print(f'Could not set clipboard for paste: {error}', file=sys.stderr)
            self._request_paste_restore(transaction)
            return
        threading.Thread(target=self._verify_paste_clipboard, args=(transaction,),
                         daemon=True).start()

    def _verify_paste_clipboard(self, transaction):
        # GTK owns the XWayland clipboard. Wait until the Wayland clipboard seen by
        # native target apps actually contains this transcript before sending Ctrl+V.
        if shutil.which('wl-paste') is None:
            GLib.idle_add(self._paste_clipboard_verified, transaction, False)
            return
        deadline = time.monotonic() + PASTE_VERIFY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(['wl-paste', '--no-newline'], capture_output=True,
                                        timeout=0.5)
                if result.returncode == 0 and result.stdout.decode('utf-8') == transaction['value']:
                    GLib.idle_add(self._paste_clipboard_verified, transaction, True)
                    return
            except (OSError, subprocess.SubprocessError, UnicodeError):
                pass
            time.sleep(0.05)
        GLib.idle_add(self._paste_clipboard_verified, transaction, False)

    def _paste_clipboard_verified(self, transaction, ready):
        if self.paste_transaction is not transaction:
            return False
        if not ready:
            self._request_paste_restore(transaction)
            return False
        try:
            command = ['sudo', '-n', 'ydotool', 'key', *PASTE_KEYS[PASTE_SHORTCUT]]
            process = Gio.Subprocess.new(
                command, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
            transaction['process'] = process
            transaction['attempted'] = True
            process.wait_async(None, self._paste_process_done, transaction)
            transaction['command_timeout'] = GLib.timeout_add(
                6000, self._paste_command_timeout, transaction)
        except (GLib.Error, OSError) as error:
            print(f'Could not start paste shortcut: {error}', file=sys.stderr)
            if transaction['process'] is not None:
                transaction['sent'] = None
            self._request_paste_restore(transaction)
        return False

    def _paste_process_done(self, process, result, transaction):
        try:
            sent = bool(process.wait_finish(result) and process.get_successful())
        except GLib.Error:
            sent = False
        if self.paste_transaction is not transaction or transaction['timed_out']:
            return
        if transaction['command_timeout'] is not None:
            GLib.source_remove(transaction['command_timeout'])
            transaction['command_timeout'] = None
        transaction['sent'] = sent
        transaction['paste_seconds'] = time.monotonic() - transaction['started']
        delay = PASTE_RESTORE_DELAY_MS if transaction['sent'] else 0
        GLib.timeout_add(delay, self._request_paste_restore, transaction)

    def _paste_command_timeout(self, transaction):
        if self.paste_transaction is transaction:
            transaction['command_timeout'] = None
            transaction['timed_out'] = True
            transaction['sent'] = None  # A partial paste may have occurred; do not type again.
            transaction['paste_seconds'] = time.monotonic() - transaction['started']
            transaction['process'].force_exit()
            self._request_paste_restore(transaction)
        return False

    def _request_paste_restore(self, transaction):
        if self.paste_transaction is not transaction:
            return False
        transaction['restore_timeout'] = GLib.timeout_add(
            2000, self._paste_restore_timeout, transaction)
        try:
            self.clipboard.request_text(self._paste_restore_received, transaction)
        except Exception as error:
            print(f'Could not inspect clipboard after paste: {error}', file=sys.stderr)
            self._finish_paste(transaction)
        return False

    def _paste_restore_received(self, clipboard, current_text, transaction):
        if self.paste_transaction is not transaction:
            return
        try:
            if current_text == transaction['value']:
                if transaction['saved_text'] is not None:
                    clipboard.set_text(transaction['saved_text'], -1)
                else:
                    clipboard.clear()
        except Exception as error:
            print(f'Could not restore clipboard after paste: {error}', file=sys.stderr)
        finally:
            self._finish_paste(transaction)

    def _paste_restore_timeout(self, transaction):
        if self.paste_transaction is transaction:
            transaction['restore_timeout'] = None
            self._finish_paste(transaction)
        return False

    def _finish_paste(self, transaction):
        if self.paste_transaction is not transaction:
            return
        if transaction['restore_timeout'] is not None:
            GLib.source_remove(transaction['restore_timeout'])
        if transaction['command_timeout'] is not None:
            GLib.source_remove(transaction['command_timeout'])
        self.paste_transaction = None
        self.clipboard_snapshot = None
        transaction['done'](transaction['sent'], transaction['paste_seconds'],
                            transaction['attempted'])

    def _type_target(self, value):
        target = self.target_accessible
        atspi_seconds = None
        if target is not None:
            started = time.monotonic()
            inserted = False
            try:
                focused = target.get_state_set().contains(Atspi.StateType.FOCUSED)
                editable = target.get_editable_text_iface() is not None
                caret = target.get_caret_offset() if editable else -1
                if focused and editable and caret >= 0:
                    inserted = bool(target.insert_text(caret, value, len(value.encode('utf-8'))))
            except Exception:
                pass
            atspi_seconds = time.monotonic() - started
            if inserted:
                return True, 'atspi', atspi_seconds, None
        mapped = value.translate(KEYMAP)
        if not mapped.isascii():
            return False, None, atspi_seconds, None
        started = time.monotonic()
        typed = False
        try:
            subprocess.run(['sudo', '-n', 'ydotool', 'type', '--key-delay', '1', '--file', '-'],
                           input=mapped, text=True, timeout=max(4, len(mapped) / 30), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            typed = True
        except (OSError, subprocess.SubprocessError):
            pass
        ydotool_seconds = time.monotonic() - started
        return typed, 'ydotool' if typed else None, atspi_seconds, ydotool_seconds

    def _press_enter(self):
        if not self._target_focused():
            return False
        try:
            subprocess.run(['sudo', '-n', 'ydotool', 'key', 'enter'], timeout=4,
                           check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def toggle(self):
        self._cancel_close_timer()
        if self.stop_event is not None:
            if self.stop_event.is_set():
                return
            self.session.format_requested = self.format_button.get_active()
            self.session.allow_hold = self.mode == 'cursor' and self.enter_button.get_active()
            if self.caret_insertion == 'paste':
                self._save_clipboard_for_paste(self.session)
            self._dismiss_mic_warning()
            self.record_seconds = time.monotonic() - self.record_started
            self.processing_started = time.monotonic()
            self.processing_seconds = None
            self._update_timing()
            self.stop_event.set()
            self._set_cancel_visible(False)
            self.recording_dot.get_style_context().remove_class('ld-dot-active')
            self._set_wave_active(False)
            self._set_wave_processing(True)
            self._stop_microphone_now()
            self.toggle_button.set_label('···')
            self.toggle_button.set_sensitive(False)
            self.status.set_text('')
            return
        self._back_from_history()
        try:
            if not AZURE_ENDPOINT:
                raise RuntimeError('Set azure_endpoint in the user config or AZURE_OPENAI_ENDPOINT')
            key = api_key()
            if self.mode == 'cursor':
                current = focused_accessible()
                if current is not None and not self.window.has_toplevel_focus():
                    self.target_accessible = current
                if bridge('IsTargetFocused') is False and self._restore_target() is False:
                    self.buffer_button.set_active(True)
                    self.status.set_text('No caret target · using Buffer')
            self.session = Session(self.mode)
            self.delivery_error = False
            self.stop_event = threading.Event()
            self.cancel_event = threading.Event()
            self.record_started = time.monotonic()
            self.record_seconds = 0.0
            self.processing_started = None
            self.processing_seconds = None
            self.transcription_seconds = None
            self.delivery_started = None
            self.delivery_seconds = None
            self.clipboard_snapshot = None
            self._update_timing()
            if self.timing_timer is not None:
                GLib.source_remove(self.timing_timer)
            self.timing_timer = GLib.timeout_add(200, self._update_timing)
            self.audio_loop = None
            self.audio_proc = None
            self.voice_frames = 0
            self._open_top_right()
            self._set_cancel_visible(True)
            if self.mode == 'cursor':
                GLib.timeout_add(150, self._restore_after_show)
            self.toggle_button.set_label('Stop · F4')
            self.history_button.set_sensitive(False)
            self.recording_dot.get_style_context().add_class('ld-dot-active')
            self._set_wave_processing(False)
            self._set_wave_active(True)
            self.status.set_text('Recording · F4 to transcribe')
            self.live.set_text('')
            self.worker = threading.Thread(target=self._run_thread,
                                           args=(key, self.session, self.stop_event, self.cancel_event),
                                           daemon=True)
            if self.music_button.get_active():
                self._pause_music()
            self.worker.start()
            self.mic_warning_timer = GLib.timeout_add(7000, self._check_initial_voice, self.session)
        except Exception as error:
            self._dismiss_mic_warning()
            self._resume_music()
            self.history_button.set_sensitive(True)
            self.status.set_text(f'Start failed: {error}')

    def _cancel_recording(self, *_args):
        if self.stop_event is None or self.stop_event.is_set():
            return
        self._dismiss_mic_warning()
        self.record_seconds = time.monotonic() - self.record_started
        self.record_started = None
        self.cancel_event.set()
        self.stop_event.set()
        self._stop_microphone_now()
        self._set_cancel_visible(False)
        self.recording_dot.get_style_context().remove_class('ld-dot-active')
        self._set_wave_active(False)
        self.toggle_button.set_label('···')
        self.toggle_button.set_sensitive(False)
        self.status.set_text('Cancelling…')

    def _stop_microphone_now(self):
        loop = self.audio_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._terminate_microphone)

    def _terminate_microphone(self):
        proc = self.audio_proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    def _run_thread(self, key, session, stop, cancel):
        result = 'complete'
        transcript = None
        error_message = None
        transcription_started = None
        transcription_seconds = None
        azure_seconds = None
        plan = None
        format_seconds = None
        try:
            try:
                asyncio.run(self._capture(session, stop))
            finally:
                GLib.idle_add(self._resume_music)
            session.close_audio()
            if cancel.is_set():
                result = 'cancelled'
                session.event('cancelled')
            else:
                transcription_started = time.monotonic()
                transcript, azure_seconds = self._transcribe_file(key, session)
                transcription_seconds = time.monotonic() - transcription_started
                session.set_transcript(transcript)
                plan = delivery_plan(transcript, session.allow_hold, session.format_requested)
                session.format_requested = plan['format_requested']
                if session.format_requested and plan['message']:
                    format_started = time.monotonic()
                    try:
                        formatted = self._format_text(key, plan['message'])
                        session.set_formatted_text(formatted)
                        plan['message'] = formatted
                    except Exception as error:
                        plan['format_error'] = str(error)
                        session.event('format_error', message=str(error))
                    finally:
                        format_seconds = time.monotonic() - format_started
        except Exception as error:
            if cancel.is_set():
                result = 'cancelled'
                session.event('cancelled')
            else:
                result = 'failed'
                error_message = str(error)
                session.event('error', message=error_message)
        finally:
            if transcription_started is not None and transcription_seconds is None:
                transcription_seconds = time.monotonic() - transcription_started
            recording_seconds = (self.record_seconds if stop.is_set() else
                                 time.monotonic() - self.record_started)
            GLib.idle_add(self._delivery_ready, session, result, recording_seconds,
                          transcription_seconds, azure_seconds, plan, format_seconds,
                          error_message)

    def _delivery_ready(self, session, result, recording_seconds, transcription_seconds,
                        azure_seconds, plan, format_seconds, error_message):
        if self.session is not session:
            return False
        self.transcription_seconds = transcription_seconds
        if result == 'complete':
            self.delivery_started = time.monotonic()
        self._update_timing()
        # Let GTK paint the completed transcription time before a slow target insertion.
        GLib.timeout_add(40, self._finish_delivery, session, result, recording_seconds,
                         transcription_seconds, azure_seconds, plan, format_seconds,
                         error_message)
        return False

    def _finish_delivery(self, session, result, recording_seconds, transcription_seconds,
                         azure_seconds, plan, format_seconds, error_message):
        if self.session is not session:
            return False
        complete = lambda metrics: self._delivery_completed(
            session, result, recording_seconds, transcription_seconds,
            azure_seconds, format_seconds, plan, metrics)
        try:
            if result == 'complete':
                self._text_event(session, plan, complete)
                return False
            if error_message is not None:
                self.status.set_text(f'Error: {error_message}')
        except Exception as error:
            self.delivery_error = True
            self.status.set_text(f'Could not deliver transcript: {error}')
        complete({'delivery_method': None, 'atspi_seconds': None,
                  'ydotool_seconds': None, 'paste_seconds': None})
        return False

    def _delivery_completed(self, session, result, recording_seconds,
                            transcription_seconds, azure_seconds, format_seconds,
                            plan, metrics):
        if self.session is not session:
            return
        self.clipboard_snapshot = None
        if self.delivery_started is not None:
            self.delivery_seconds = time.monotonic() - self.delivery_started
        try:
            session.finish(result)
        except Exception as error:
            self.delivery_error = True
            self.status.set_text(f'Could not save session: {error}')
        processing_seconds = (time.monotonic() - self.processing_started
                              if self.processing_started is not None else None)
        self._finished(result, recording_seconds, processing_seconds)
        try:
            append_timing(session, recording_seconds, processing_seconds, result,
                          transcription_seconds=transcription_seconds,
                          azure_seconds=azure_seconds,
                          format_seconds=format_seconds,
                          format_error=bool(plan and plan['format_error']),
                          delivery_seconds=self.delivery_seconds,
                          atspi_seconds=metrics['atspi_seconds'],
                          ydotool_seconds=metrics['ydotool_seconds'],
                          paste_seconds=metrics['paste_seconds'],
                          delivery_method=metrics['delivery_method'],
                          delivery_error=self.delivery_error)
        except OSError as error:
            print(f'Could not save dictation timing: {error}', file=sys.stderr)

    def _restore_after_show(self):
        if not self._restore_target() and self.window.has_toplevel_focus():
            self.status.set_text('Caret target unavailable · switch to Buffer')
        return False

    async def _capture(self, session, stop):
        proc = await asyncio.create_subprocess_exec('parec', '--raw', '--format=s16le',
            '--rate=24000', '--channels=1', stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        self.audio_loop = asyncio.get_running_loop()
        self.audio_proc = proc
        try:
            while not stop.is_set():
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(4800), .5)
                except asyncio.TimeoutError:
                    continue
                if not chunk:
                    if stop.is_set():
                        break
                    raise RuntimeError('Microphone capture stopped')
                meter_bytes = chunk[:len(chunk) & ~1]
                rms = audioop.rms(meter_bytes, 2) if meter_bytes else 0
                level = min(1.0, max(0.0, (rms - 75) / 550))
                GLib.idle_add(self._set_wave_level, session, level, rms)
                session.audio.writeframesraw(chunk)
        finally:
            self._terminate_microphone()
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            self.audio_proc = None
            self.audio_loop = None

    def _transcribe_file(self, key, session):
        audio_path = session.path / 'audio.wav'
        with wave.open(str(audio_path), 'rb') as recorded:
            if recorded.getnframes() == 0:
                raise RuntimeError('No audio captured')
        with tempfile.TemporaryDirectory(prefix='live-dictation-upload-') as temporary:
            upload = audio_path
            # The file API accepts at most 25 MB. Keep the original WAV in
            # history and compress only the copy sent for a long recording.
            if audio_path.stat().st_size > 24_000_000:
                upload = pathlib.Path(temporary) / 'audio.mp3'
                try:
                    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                                    '-i', str(audio_path), '-ac', '1', '-b:a', '48k',
                                    str(upload)], check=True, timeout=120,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                except (OSError, subprocess.SubprocessError) as error:
                    raise RuntimeError(f'Could not prepare long audio for upload: {error}') from error
            if upload.stat().st_size > 24_000_000:
                raise RuntimeError('Recording exceeds the 25 MB transcription limit; audio saved in History')
            boundary = '----live-dictation-' + uuid.uuid4().hex
            name = upload.name
            mime = 'audio/mpeg' if upload.suffix == '.mp3' else 'audio/wav'
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n'
                    f'{DEPLOYMENT}\r\n--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                    f'Content-Type: {mime}\r\n\r\n').encode() + upload.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
            version = urllib.parse.urlencode({'api-version': API_VERSION})
            url = (f'{AZURE_ENDPOINT}/openai/deployments/{urllib.parse.quote(DEPLOYMENT, safe="")}'
                   f'/audio/transcriptions?{version}')
            request = urllib.request.Request(url, data=body, method='POST', headers={
                'api-key': key, 'Content-Type': f'multipart/form-data; boundary={boundary}'})
            azure_started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=300) as response:
                    payload = json.load(response)
            except urllib.error.HTTPError as error:
                try:
                    detail = json.loads(error.read(2048)).get('error', {}).get('message', '')
                except (ValueError, AttributeError):
                    detail = ''
                raise RuntimeError(f'Azure transcription failed ({error.code}): {detail or error.reason}') from error
            except urllib.error.URLError as error:
                raise RuntimeError(f'Azure transcription connection failed: {error.reason}') from error
            transcript = payload.get('text')
            if not isinstance(transcript, str):
                raise RuntimeError('Azure transcription returned no text field')
            return transcript.strip(), time.monotonic() - azure_started

    @staticmethod
    def _format_text(key, text):
        if not FORMAT_DEPLOYMENT:
            raise RuntimeError('Set format_deployment in the user config')
        version = urllib.parse.urlencode({'api-version': FORMAT_API_VERSION})
        url = (f'{AZURE_ENDPOINT}/openai/deployments/'
               f'{urllib.parse.quote(FORMAT_DEPLOYMENT, safe="")}/chat/completions?{version}')
        body = json.dumps({'messages': [
            {'role': 'system', 'content': FORMAT_PROMPT},
            {'role': 'user', 'content': text},
        ], 'temperature': 0.2}, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(url, data=body, method='POST', headers={
            'api-key': key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(2048)).get('error', {}).get('message', '')
            except (ValueError, AttributeError):
                detail = ''
            raise RuntimeError(
                f'Azure formatting failed ({error.code}): {detail or error.reason}') from error
        except urllib.error.URLError as error:
            raise RuntimeError(f'Azure formatting connection failed: {error.reason}') from error
        choices = payload.get('choices')
        if not isinstance(choices, list) or not choices:
            raise RuntimeError('Azure formatting returned no choice')
        choice = choices[0]
        if choice.get('finish_reason') != 'stop':
            raise RuntimeError('Azure formatting did not finish cleanly')
        content = choice.get('message', {}).get('content')
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError('Azure formatting returned no text')
        return re.sub(r'[ \t]*—+[ \t]*', ' ', content).strip()

    def _text_event(self, session, plan, done):
        metrics = {'delivery_method': None, 'atspi_seconds': None,
                   'ydotool_seconds': None, 'paste_seconds': None}
        if self.session is not session:
            return
        message = plan['message']
        cue = plan['cue']
        if cue == 'hold' and not message:
            done(metrics)
            return
        if not message:
            self.live.set_text('')
            self.status.set_text('No text to insert · audio saved in History')
            self.delivery_error = True
            done(metrics)
            return
        self.live.set_text(message[-300:])
        if plan['format_error']:
            self.delivery_error = True
            self.status.set_text('Formatting failed · using original text')
        send_after = (not plan['format_requested'] and
                      (cue == 'send' or
                       (self.enter_button.get_active() and cue not in ('hold', 'format'))))
        if self.mode == 'buffer':
            buffer = self.text.get_buffer()
            buffer.insert(buffer.get_end_iter(), message)
            metrics['delivery_method'] = 'buffer'
        else:
            if not self._target_focused():
                self.delivery_error = True
                buffer = self.text.get_buffer()
                buffer.insert(buffer.get_end_iter(), message)
                metrics['delivery_method'] = 'buffer'
                self.buffer_button.set_active(True)
                self.status.set_text('Caret focus changed · transcript kept in Buffer')
                done(metrics)
                return
            if self.caret_insertion == 'paste':
                self._paste_target(
                    session, message,
                    lambda sent, seconds, attempted: self._paste_completed(
                        session, message, send_after, metrics, done,
                        sent, seconds, attempted))
                return
            typed, method, atspi_seconds, ydotool_seconds = self._type_target(message)
            metrics['atspi_seconds'] = atspi_seconds
            metrics['ydotool_seconds'] = ydotool_seconds
            if not typed:
                self.delivery_error = True
                buffer = self.text.get_buffer()
                buffer.insert(buffer.get_end_iter(), message)
                metrics['delivery_method'] = 'buffer'
                self.buffer_button.set_active(True)
                self.status.set_text('Could not type at caret · transcript kept in Buffer')
            else:
                metrics['delivery_method'] = method
            if typed and send_after and not self._press_enter():
                self.delivery_error = True
                self.status.set_text('Text inserted · Enter could not be pressed')
        done(metrics)

    def _paste_completed(self, session, message, send_after, metrics, done,
                         sent, seconds, attempted):
        if self.session is not session:
            return
        metrics['paste_seconds'] = seconds
        try:
            if sent is True:
                metrics['delivery_method'] = 'paste'
                if send_after and not self._press_enter():
                    self.delivery_error = True
                    self.status.set_text('Text pasted · Enter could not be pressed')
            elif sent is None:
                self.delivery_error = True
                metrics['delivery_method'] = 'paste-uncertain'
                self.status.set_text('Paste result uncertain · transcript saved in History')
            elif not attempted:
                typed, method, atspi_seconds, ydotool_seconds = self._type_target(message)
                metrics['atspi_seconds'] = atspi_seconds
                metrics['ydotool_seconds'] = ydotool_seconds
                if typed:
                    metrics['delivery_method'] = method
                    if send_after and not self._press_enter():
                        self.delivery_error = True
                        self.status.set_text('Text inserted · Enter could not be pressed')
                else:
                    self.delivery_error = True
                    buffer = self.text.get_buffer()
                    buffer.insert(buffer.get_end_iter(), message)
                    metrics['delivery_method'] = 'buffer'
                    self.buffer_button.set_active(True)
                    self.status.set_text('Clipboard was not ready · transcript kept in Buffer')
            else:
                self.delivery_error = True
                buffer = self.text.get_buffer()
                buffer.insert(buffer.get_end_iter(), message)
                metrics['delivery_method'] = 'buffer'
                self.buffer_button.set_active(True)
                self.status.set_text('Paste shortcut failed · transcript kept in Buffer')
        except Exception as error:
            self.delivery_error = True
            self.status.set_text(f'Could not finish paste: {error}')
        finally:
            done(metrics)

    def _finished(self, result, recording_seconds, processing_seconds):
        self._dismiss_mic_warning()
        self._resume_music()
        if self.timing_timer is not None:
            GLib.source_remove(self.timing_timer)
            self.timing_timer = None
        self.record_seconds = recording_seconds
        self.record_started = None
        self.processing_seconds = processing_seconds
        self._update_timing()
        self.stop_event = None
        self.cancel_event = None
        self.session = None
        self.worker = None
        self.recording_dot.get_style_context().remove_class('ld-dot-active')
        self._set_wave_processing(False)
        self._set_wave_active(False)
        self.toggle_button.set_label('Start · F4')
        self.toggle_button.set_sensitive(True)
        self.history_button.set_sensitive(True)
        self._set_cancel_visible(False)
        if result == 'complete':
            if not self.delivery_error:
                self.status.set_text('Saved · F4 to start again')
            if self.mode == 'cursor' and not self.delivery_error:
                self.close_timer = GLib.timeout_add(3000, self._hide_completed)
        elif result == 'cancelled':
            self.status.set_text('Ready · F4 to start')
        return False


if __name__ == '__main__':
    app = Dictation()
    Gtk.main()
