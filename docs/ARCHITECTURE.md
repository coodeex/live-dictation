# How Live Dictation works

```text
Shortcut / button → client.py → local Unix socket → GTK app
                                               │
Microphone (parec, 24 kHz PCM) → local audio.wav
                                               │ after F4 stops recording
                                               ▼
                              Azure file transcription
                                               │ completed text only
                                               ▼
                               Caret target or editable Buffer
                                               │
                                               ▼
                              Local audio + transcript history
```

## The desktop process

`app.py` owns the GTK 3 popup and a user-only Unix socket under `XDG_RUNTIME_DIR`. `client.py` sends commands to that process, so the shortcut does not create duplicate recorders. The popup uses XWayland's always-on-top window state and can be dragged by its title area.

GTK widgets are updated on the GTK thread. Microphone capture runs in a worker thread and writes a local WAV while the audio meter observes the same PCM chunks. F4 stops recording and transcribes; silence does not stop it. The Cancel button appears only during recording and stops capture without uploading or inserting text. Cancelled audio remains in History. After F4, the worker sends the completed file to Azure's audio transcription endpoint and sends the final transcript back to GTK through `GLib.idle_add`.

## Two destinations

**Caret** inserts the completed transcript into the original focused field. The app first tries AT-SPI's editable text interface. For fields that do not expose one, it can fall back to `ydotool` with an optional per-user keyboard map. If the target cannot be confirmed or typed into, the transcript is placed in Buffer and the popup stays open for copying.

The small **↵** toggle is off by default. In Caret mode, enabling it presses the Enter key after successful text insertion, provided the original target still has focus. The target application decides whether Enter sends the message or inserts a line break. The setting persists between sessions and has no effect in Buffer mode.

The **♫** toggle is also off by default and persists between sessions. When recording starts, it pauses MPRIS players that are currently playing and remembers which player instances it paused. It resumes only those same instances after microphone capture stops, including when the recording is cancelled or capture fails. Turning the toggle on during recording pauses music then; turning it off resumes music it paused.

**Buffer** appends the completed transcript to the GTK text editor. You can edit the result and copy it from the icon in the editor. The destination selected when transcription finishes receives the text. The popup shows elapsed recording time beside the waveform and, after F4, elapsed time from F4 to completed transcription. Caret mode shows both final times for three seconds before closing; Buffer mode leaves them visible for review and copying.

An optional GNOME Shell [focus helper](../extension/) remembers and restores the original app window for Caret mode. The shell extension does not bind the recording shortcut; your desktop's custom keyboard shortcut launches `client.py toggle`.

## What stays on disk

Runtime data lives in `~/.local/share/live-dictation/`, outside the Git clone:

| Path | Purpose |
| :-- | :-- |
| `settings.json` | Last selected destination and Enter toggle |
| `buffer-draft.txt` | Editable buffer between restarts |
| `timings.jsonl` | Append-only recording and processing durations, with session ID and result |
| `history/<timestamp-id>/audio.wav` | Recorded microphone audio |
| `history/<timestamp-id>/transcript.txt` | Exact finalized transcript |
| `history/<timestamp-id>/events.jsonl` | Session and final transcription events |
| `history/<timestamp-id>/metadata.json` | Session time, endpoint, model, and result |

History is retained until you remove it. The data directory and files are created with user-only permissions. The API key is read from the process environment or a private file named in `~/.config/live-dictation/config.json`; its value is not written to history.

Audio is sent to your configured Azure OpenAI resource only after recording stops. The file endpoint accepts up to 25 MB. For longer WAV recordings, the app creates a temporary compressed MP3 for upload while keeping the original WAV in History. The finalized wording is preserved in `transcript.txt`.
