#!/usr/bin/env python3
"""Azure live dictation, with a GTK buffer and durable per-session history."""
import asyncio
import audioop
import datetime as dt
import json
import math
import os
import pathlib
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
import wave

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Atspi', '2.0')
gi.require_version('Pango', '1.0')
from gi.repository import Atspi, Gdk, GLib, Gtk, Pango

ROOT = pathlib.Path.home() / '.local/share/live-dictation'
HISTORY = ROOT / 'history'
SETTINGS = ROOT / 'settings.json'
DRAFT = ROOT / 'buffer-draft.txt'
TIMINGS = ROOT / 'timings.jsonl'
STYLE = pathlib.Path(__file__).with_name('style.css')
SOCKET = pathlib.Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'live-dictation.sock'
CONFIG_HOME = pathlib.Path(os.environ.get(
    'XDG_CONFIG_HOME', pathlib.Path.home() / '.config')).expanduser()
CONFIG_PATH = pathlib.Path(os.environ.get(
    'LIVE_DICTATION_CONFIG', CONFIG_HOME / 'live-dictation/config.json')).expanduser()
BRIDGE = 'org.gnome.Shell.Extensions.LiveDictation'
BRIDGE_PATH = '/org/gnome/Shell/Extensions/LiveDictation'


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
KEYMAP = str.maketrans(CONFIG.get('ydotool_keymap', {}))


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


def append_timing(session, recording_seconds, processing_seconds, status):
    entry = {
        'session': session.path.name,
        'started': session.started,
        'status': status,
        'recording_seconds': round(recording_seconds, 3),
        'processing_seconds': (round(processing_seconds, 3)
                               if processing_seconds is not None else None),
    }
    descriptor = os.open(TIMINGS, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, 'a', encoding='utf-8') as log:
        log.write(json.dumps(entry, ensure_ascii=False) + '\n')
    os.chmod(TIMINGS, 0o600)


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
        self.audio_closed = False
        self.status = 'recording'
        self.write_metadata()

    def write_metadata(self):
        atomic_text(self.path / 'metadata.json', json.dumps({
            'started': self.started, 'ended': getattr(self, 'ended', None),
            'model': DEPLOYMENT, 'endpoint': AZURE_ENDPOINT, 'status': self.status,
            'initial_mode': self.mode}, indent=2) + '\n')

    def event(self, kind, **fields):
        self.events.write(json.dumps({'time': dt.datetime.now().astimezone().isoformat(),
                                      'kind': kind, **fields}, ensure_ascii=False) + '\n')

    def set_transcript(self, text):
        self.final_text = text
        self.event('final', text=text)
        self.save_transcript()

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
        self.prefs = {'mode': 'cursor', 'press_enter': False}
        if SETTINGS.exists():
            try:
                self.prefs.update(json.loads(SETTINGS.read_text()))
            except (ValueError, OSError):
                pass
        self.mode = self.prefs.get('mode', 'cursor')
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
        self.copy_feedback_timer = None
        self.timing_timer = None
        self.close_timer = None
        self.record_started = None
        self.record_seconds = 0.0
        self.processing_started = None
        self.processing_seconds = None
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
        w = self.window = Gtk.Window(title='Live dictation')
        w.set_wmclass('live-dictation', 'live-dictation')
        w.set_default_size(534, 334)
        w.set_size_request(534, 334)
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
        w.connect('delete-event', self._minimize)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.get_style_context().add_class('ld-card')
        w.add(card)

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.pack_start(header, False, False, 0)
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
        title = Gtk.Label(label='Live dictation')
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
        close = Gtk.Button(label='−')
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_can_focus(False)
        close.set_size_request(22, 22)
        close.set_tooltip_text('Minimize popup')
        close.get_style_context().add_class('ld-close')
        close.connect('clicked', self._minimize)
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
        card.pack_start(live_row, False, False, 0)
        self.timing = Gtk.Label(label='0:00')
        self.timing.set_xalign(0)
        self.timing.set_width_chars(13)
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
        card.pack_start(editor_overlay, True, True, 0)
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
        self.text.set_right_margin(44)
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
        self.copy_button.set_halign(Gtk.Align.END)
        self.copy_button.set_valign(Gtk.Align.START)
        self.copy_button.set_margin_top(8)
        self.copy_button.set_margin_end(8)
        self.copy_button.set_tooltip_text('Copy buffer')
        self.copy_button.get_style_context().add_class('ld-copy-icon')
        self.copy_button.connect('clicked', self._copy)
        editor_overlay.add_overlay(self.copy_button)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        card.pack_start(buttons, False, False, 0)
        self.cancel_button = Gtk.Button(label='Cancel')
        self.cancel_button.set_no_show_all(True)
        self.cancel_button.get_style_context().add_class('ld-ghost')
        self.cancel_button.connect('clicked', self._cancel_recording)
        buttons.pack_start(self.cancel_button, False, False, 0)
        buttons.pack_start(Gtk.Box(), True, True, 0)
        history = Gtk.Button(label='History')
        history.connect('clicked', lambda *_: subprocess.Popen(['xdg-open', str(HISTORY)]))
        history.get_style_context().add_class('ld-ghost')
        buttons.pack_start(history, False, False, 0)
        self.toggle_button = Gtk.Button(label='Start · F4')
        self.toggle_button.connect('clicked', lambda *_: self.toggle())
        self.toggle_button.get_style_context().add_class('ld-primary')
        buttons.pack_start(self.toggle_button, False, False, 0)
        self.window.show_all()
        self.window.hide()

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

    def _set_wave_level(self, session, level):
        if self.session is session and self.wave_active:
            self.wave_level = level
        return False

    def _animate_waveform(self):
        if not self.wave_active and not self.wave_processing:
            self.wave_timer = None
            return False
        self.wave_phase += 0.13 if self.wave_processing else 0.21
        if self.wave_active:
            self.wave_display += (max(0.26, self.wave_level) - self.wave_display) * 0.32
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
            processing = (time.monotonic() - self.processing_started
                          if self.processing_seconds is None else self.processing_seconds)
            display += ' · ' + self._clock(processing)
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
                elif command == 'status':
                    gdk_window = self.window.get_window()
                    connection.sendall(json.dumps({'mode': self.mode,
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

    def _minimize(self, *_args):
        self.window.iconify()
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

    def _open_top_right(self):
        position = self._top_right_position()
        self._move_window(*position)
        self.window.show_all()
        self.window.deiconify()
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
        self.status.set_text('Buffer copied')
        self.copy_icon.set_from_icon_name('emblem-ok-symbolic', Gtk.IconSize.MENU)
        self.copy_button.get_style_context().add_class('ld-copy-success')
        if self.copy_feedback_timer is not None:
            GLib.source_remove(self.copy_feedback_timer)
        self.copy_feedback_timer = GLib.timeout_add(2000, self._reset_copy_icon)

    def _reset_copy_icon(self):
        self.copy_icon.set_from_icon_name('edit-copy-symbolic', Gtk.IconSize.MENU)
        self.copy_button.get_style_context().remove_class('ld-copy-success')
        self.copy_feedback_timer = None
        return False

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

    def _type_target(self, value):
        target = self.target_accessible
        if target is not None:
            try:
                focused = target.get_state_set().contains(Atspi.StateType.FOCUSED)
                editable = target.get_editable_text_iface() is not None
                caret = target.get_caret_offset() if editable else -1
                if focused and editable and caret >= 0:
                    if target.insert_text(caret, value, len(value.encode('utf-8'))):
                        return True
            except Exception:
                pass
        mapped = value.translate(KEYMAP)
        if not mapped.isascii():
            return False
        try:
            subprocess.run(['sudo', '-n', 'ydotool', 'type', '--key-delay', '1', '--file', '-'],
                           input=mapped, text=True, timeout=max(4, len(mapped) / 30), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

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
            self._update_timing()
            if self.timing_timer is not None:
                GLib.source_remove(self.timing_timer)
            self.timing_timer = GLib.timeout_add(200, self._update_timing)
            self.audio_loop = None
            self.audio_proc = None
            self._open_top_right()
            self._set_cancel_visible(True)
            if self.mode == 'cursor':
                GLib.timeout_add(150, self._restore_after_show)
            self.toggle_button.set_label('Stop · F4')
            self.recording_dot.get_style_context().add_class('ld-dot-active')
            self._set_wave_processing(False)
            self._set_wave_active(True)
            self.status.set_text('Recording · F4 to transcribe')
            self.live.set_text('')
            self.worker = threading.Thread(target=self._run_thread,
                                           args=(key, self.session, self.stop_event, self.cancel_event),
                                           daemon=True)
            self.worker.start()
        except Exception as error:
            self.status.set_text(f'Start failed: {error}')

    def _cancel_recording(self, *_args):
        if self.stop_event is None or self.stop_event.is_set():
            return
        self.record_seconds = time.monotonic() - self.record_started
        self.record_started = None
        self.cancel_event.set()
        self.stop_event.set()
        self._stop_microphone_now()
        self._set_cancel_visible(False)
        self.recording_dot.get_style_context().remove_class('ld-dot-active')
        self._set_wave_active(False)
        self.toggle_button.set_sensitive(False)
        self.window.hide()

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
        try:
            asyncio.run(self._capture(session, stop))
            session.close_audio()
            if cancel.is_set():
                result = 'cancelled'
                session.event('cancelled')
            else:
                transcript = self._transcribe_file(key, session)
                session.set_transcript(transcript)
                GLib.idle_add(self._text_event, session, transcript)
        except Exception as error:
            if cancel.is_set():
                result = 'cancelled'
                session.event('cancelled')
            else:
                result = 'failed'
                session.event('error', message=str(error))
                GLib.idle_add(self.status.set_text, f'Error: {error}')
        finally:
            recording_seconds = (self.record_seconds if stop.is_set() else
                                 time.monotonic() - self.record_started)
            processing_seconds = (time.monotonic() - self.processing_started
                                  if self.processing_started is not None else None)
            session.finish(result)
            try:
                append_timing(session, recording_seconds, processing_seconds, result)
            except OSError as error:
                print(f'Could not save dictation timing: {error}', file=sys.stderr)
            GLib.idle_add(self._finished, result, recording_seconds, processing_seconds)

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
                level = min(1.0, max(0.0, (rms - 160) / 3500))
                GLib.idle_add(self._set_wave_level, session, level)
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
            return transcript.strip()

    def _text_event(self, session, transcript):
        if self.session is not session:
            return False
        self.live.set_text(transcript[-300:])
        if not transcript:
            self.status.set_text('No speech recognized · audio saved in History')
            self.delivery_error = True
            return False
        if self.mode == 'buffer':
            buffer = self.text.get_buffer()
            buffer.insert(buffer.get_end_iter(), transcript)
        else:
            if not self._target_focused():
                self.delivery_error = True
                buffer = self.text.get_buffer()
                buffer.insert(buffer.get_end_iter(), transcript)
                self.buffer_button.set_active(True)
                self.status.set_text('Caret focus changed · transcript kept in Buffer')
                return False
            if not self._type_target(transcript):
                self.delivery_error = True
                buffer = self.text.get_buffer()
                buffer.insert(buffer.get_end_iter(), transcript)
                self.buffer_button.set_active(True)
                self.status.set_text('Could not type at caret · transcript kept in Buffer')
            elif self.enter_button.get_active() and not self._press_enter():
                self.delivery_error = True
                self.status.set_text('Text inserted · Enter could not be pressed')
        return False

    def _finished(self, result, recording_seconds, processing_seconds):
        if self.timing_timer is not None:
            GLib.source_remove(self.timing_timer)
            self.timing_timer = None
        self.record_seconds = recording_seconds
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
        self._set_cancel_visible(False)
        if result == 'complete':
            if not self.delivery_error:
                self.status.set_text('Saved · F4 to start again')
            if self.mode == 'cursor' and not self.delivery_error:
                self.close_timer = GLib.timeout_add(3000, self._hide_completed)
        elif result == 'cancelled':
            self.status.set_text('Ready · F4 to start')
            self.window.hide()
        return False


if __name__ == '__main__':
    app = Dictation()
    Gtk.main()
