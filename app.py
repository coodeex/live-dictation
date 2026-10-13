#!/usr/bin/env python3
"""Azure live dictation, with a GTK buffer and durable per-session history."""
import asyncio
import audioop
import base64
import datetime as dt
import json
import math
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import uuid
import wave

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Atspi', '2.0')
gi.require_version('Pango', '1.0')
from gi.repository import Atspi, Gdk, GLib, Gtk, Pango
import websockets

ROOT = pathlib.Path.home() / '.local/share/live-dictation'
HISTORY = ROOT / 'history'
SETTINGS = ROOT / 'settings.json'
DRAFT = ROOT / 'buffer-draft.txt'
STYLE = pathlib.Path(__file__).with_name('style.css')
SOCKET = pathlib.Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'live-dictation.sock'
AZURE_ENDPOINT = 'https://galactic-azure-openai.openai.azure.com'
DEPLOYMENT = 'gpt-live-transcribe'
BRIDGE = 'org.gnome.Shell.Extensions.LiveDictation'
BRIDGE_PATH = '/org/gnome/Shell/Extensions/LiveDictation'
KEYMAP = str.maketrans({"'": "\\", '?': '_', ':': '>', ';': '<', '"': '@',
                        'ä': "'", 'ö': ';', 'å': '[', 'Ä': '"', 'Ö': ':', 'Å': '{'})


def api_key():
    path = pathlib.Path.home() / '.codex/.env'
    value = None
    with path.open() as source:
        for line in source:
            if line.startswith('AZURE_OPENAI_API_KEY='):
                value = line.partition('=')[2].strip().strip('"').strip("'")
    if not value:
        raise RuntimeError('AZURE_OPENAI_API_KEY is missing from ~/.codex/.env')
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
        self.parts = {}
        self.order = []
        self.commit_count = 0
        self.completed_count = 0
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

    def update(self, item, delta=None, final=None):
        self.ensure_item(item)
        part = self.parts[item]
        if delta is not None:
            part['text'] += delta
            self.event('delta', item_id=item, text=delta)
        if final is not None:
            part['text'] = final
            part['final'] = True
            self.event('final', item_id=item, text=final)
        self.save_transcript()
        return part['text']

    def ensure_item(self, item):
        if item not in self.parts:
            self.parts[item] = {'text': '', 'final': False}
            self.order.append(item)

    def transcript(self):
        return ' '.join(self.parts[item]['text'].strip() for item in self.order
                        if self.parts[item]['text'].strip())

    def save_transcript(self):
        atomic_text(self.path / 'transcript.txt', self.transcript() + '\n')

    def finish(self, status):
        self.status = status
        self.ended = dt.datetime.now().astimezone().isoformat()
        self.audio.close()
        self.save_transcript()
        self.write_metadata()
        self.events.close()


class Dictation:
    def __init__(self):
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        HISTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ROOT, 0o700)
        os.chmod(HISTORY, 0o700)
        self.prefs = {'mode': 'cursor'}
        if SETTINGS.exists():
            try:
                self.prefs.update(json.loads(SETTINGS.read_text()))
            except (ValueError, OSError):
                pass
        self.mode = self.prefs.get('mode', 'cursor')
        self.session = None
        self.stop_event = None
        self.worker = None
        self.audio_loop = None
        self.audio_proc = None
        self.target_accessible = None
        self.wave_level = 0.0
        self.wave_display = 0.0
        self.wave_phase = 0.0
        self.wave_active = False
        self.wave_timer = None
        self.copy_feedback_timer = None
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
        close = Gtk.Button(label='✕')
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
            bar_height = 3 + level * (height - 5) * shape * pulse
            x = left + index * (bar_width + gap)
            y = (height - bar_height) / 2
            radius = bar_width / 2
            context.new_sub_path()
            context.arc(x + radius, y + radius, radius, math.pi, 2 * math.pi)
            context.arc(x + radius, y + bar_height - radius, radius, 0, math.pi)
            context.close_path()
            if self.wave_active:
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

    def _set_wave_level(self, session, level):
        if self.session is session and self.wave_active:
            self.wave_level = level
        return False

    def _animate_waveform(self):
        if not self.wave_active:
            self.wave_timer = None
            return False
        self.wave_phase += 0.21
        self.wave_display += (max(0.26, self.wave_level) - self.wave_display) * 0.32
        self.waveform.queue_draw()
        return True

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
                    self.window.show_all()
                    self.window.deiconify()
                    self._ensure_topmost()
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

    def _save_prefs(self):
        self.prefs['mode'] = self.mode
        atomic_text(SETTINGS, json.dumps(self.prefs, indent=2) + '\n')
        os.chmod(SETTINGS, 0o600)

    def _mode_changed(self, button):
        if not button.get_active():
            return
        self.mode = 'cursor' if button is self.cursor_button else 'buffer'
        if self.session:
            self.session.event('destination', mode=self.mode)
        self._save_prefs()
        if self.mode == 'cursor':
            self.window.hide()
            self.window.set_accept_focus(False)
            self.window.show_all()
            self._ensure_topmost()
            restored = self._restore_target()
            if restored is False:
                self.status.set_text('Caret target unavailable · switch to Buffer')
        else:
            self.window.set_accept_focus(True)
            self.window.present()

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
                           input=mapped, text=True, timeout=4, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def toggle(self):
        if self.stop_event is not None:
            self.stop_event.set()
            self.recording_dot.get_style_context().remove_class('ld-dot-active')
            self._set_wave_active(False)
            self._stop_microphone_now()
            if self.mode != 'buffer':
                self.window.hide()
            return
        try:
            key = api_key()
            if self.mode == 'cursor':
                current = focused_accessible()
                if current is not None and not self.window.has_toplevel_focus():
                    self.target_accessible = current
                if bridge('IsTargetFocused') is False and self._restore_target() is False:
                    self.buffer_button.set_active(True)
                    self.status.set_text('No caret target · using Buffer')
            self.session = Session(self.mode)
            self._output_emitted = {}
            self.stop_event = threading.Event()
            self.audio_loop = None
            self.audio_proc = None
            self.window.show_all()
            self.window.deiconify()
            self._ensure_topmost()
            if self.mode == 'cursor':
                GLib.timeout_add(150, self._restore_after_show)
            self.toggle_button.set_label('Stop · F4')
            self.recording_dot.get_style_context().add_class('ld-dot-active')
            self._set_wave_active(True)
            self.status.set_text('Connecting to Azure…')
            self.live.set_text('')
            self.worker = threading.Thread(target=self._run_thread, args=(key, self.session, self.stop_event), daemon=True)
            self.worker.start()
        except Exception as error:
            self.status.set_text(f'Start failed: {error}')

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

    def _run_thread(self, key, session, stop):
        result = 'complete'
        try:
            asyncio.run(self._stream(key, session, stop))
        except Exception as error:
            result = 'failed'
            session.event('error', message=str(error))
            GLib.idle_add(self.status.set_text, f'Error: {error}')
        finally:
            session.finish(result)
            GLib.idle_add(self._finished, result)

    def _restore_after_show(self):
        if not self._restore_target() and self.window.has_toplevel_focus():
            self.status.set_text('Caret target unavailable · switch to Buffer')
        return False

    async def _stream(self, key, session, stop):
        url = AZURE_ENDPOINT.replace('https://', 'wss://') + '/openai/v1/realtime?intent=transcription'
        async with websockets.connect(url, additional_headers={'api-key': key}, open_timeout=12) as ws:
            await ws.send(json.dumps({'type': 'session.update', 'session': {
                'type': 'transcription', 'audio': {'input': {
                    'format': {'type': 'audio/pcm', 'rate': 24000},
                    'turn_detection': None,
                    'transcription': {'model': DEPLOYMENT, 'delay': 'low'}}}}}))
            for _ in range(5):
                event = json.loads(await asyncio.wait_for(ws.recv(), 12))
                if event.get('type') == 'error':
                    raise RuntimeError(event.get('error', {}).get('message', 'Azure session error'))
                if event.get('type') == 'session.updated':
                    break
            else:
                raise RuntimeError('Azure did not configure the transcription session')
            if stop.is_set():
                return
            GLib.idle_add(self.status.set_text, 'Recording · F4 to stop')
            proc = await asyncio.create_subprocess_exec('parec', '--raw', '--format=s16le',
                '--rate=24000', '--channels=1', stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            self.audio_loop = asyncio.get_running_loop()
            self.audio_proc = proc
            receiver = asyncio.create_task(self._receive(ws, session))
            uncommitted = 0
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
                    await ws.send(json.dumps({'type': 'input_audio_buffer.append',
                                              'audio': base64.b64encode(chunk).decode('ascii')}))
                    uncommitted += len(chunk)
                    if uncommitted >= 24000 * 2 * 3:
                        await ws.send(json.dumps({'type': 'input_audio_buffer.commit'}))
                        session.commit_count += 1
                        uncommitted = 0
                    if receiver.done():
                        await receiver
                self._terminate_microphone()
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                self.audio_proc = None
                self.audio_loop = None
                if uncommitted:
                    await ws.send(json.dumps({'type': 'input_audio_buffer.commit'}))
                    session.commit_count += 1
                deadline = time.monotonic() + 12
                while session.completed_count < session.commit_count and time.monotonic() < deadline:
                    if receiver.done():
                        await receiver
                    await asyncio.sleep(.1)
                if session.completed_count < session.commit_count:
                    session.event('final_timeout', pending=session.commit_count - session.completed_count)
            finally:
                self._terminate_microphone()
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                self.audio_proc = None
                self.audio_loop = None
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)

    async def _receive(self, ws, session):
        async for raw in ws:
            event = json.loads(raw)
            kind = event.get('type', '')
            if kind == 'error':
                raise RuntimeError(event.get('error', {}).get('message', 'Azure error'))
            item = event.get('item_id')
            if kind == 'input_audio_buffer.committed' and item:
                session.ensure_item(item)
            elif kind == 'conversation.item.input_audio_transcription.delta' and item:
                delta = event.get('delta', '')
                if delta:
                    session.update(item, delta=delta)
                    self._queue_text(session, item, final=False)
            elif kind == 'conversation.item.input_audio_transcription.completed' and item:
                final = event.get('transcript', '')
                session.update(item, final=final)
                session.completed_count += 1
                self._queue_text(session, item, final=True)
            elif kind == 'conversation.item.input_audio_transcription.failed':
                session.completed_count += 1
                session.event('transcription_failed', detail=event.get('error', {}))
                GLib.idle_add(self.status.set_text, 'Transcription segment failed · audio saved')

    def _queue_text(self, session, item, final):
        prefix = '' if session.order[0] == item else ' '
        candidate = prefix + session.parts[item]['text'].lstrip()
        if not final:
            candidate = candidate.rstrip('.,!?;:')
        GLib.idle_add(self._text_event, session, item, candidate, session.transcript())

    def _text_event(self, session, item, candidate, transcript):
        if self.session is not session:
            return False
        self.live.set_text(transcript[-300:])
        emitted = self._output_emitted.get(item, '')
        if not candidate.startswith(emitted):
            self.status.set_text('Final wording revised · exact text saved in History')
            return False
        delta = candidate[len(emitted):]
        if not delta:
            return False
        if self.mode == 'buffer':
            buffer = self.text.get_buffer()
            buffer.insert(buffer.get_end_iter(), delta)
            self._output_emitted[item] = candidate
        else:
            if not self._target_focused():
                self.status.set_text('Caret focus changed · text saved in History')
                return False
            if self._type_target(delta):
                self._output_emitted[item] = candidate
            else:
                self.status.set_text('Could not type at cursor · text saved in History')
        return False

    def _finished(self, result):
        self.stop_event = None
        self.session = None
        self.worker = None
        self.recording_dot.get_style_context().remove_class('ld-dot-active')
        self._set_wave_active(False)
        self.toggle_button.set_label('Start · F4')
        if result == 'complete':
            self.status.set_text('Saved · F4 to start again')
        return False


if __name__ == '__main__':
    app = Dictation()
    Gtk.main()
