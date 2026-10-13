# How Live Dictation works

```text
Shortcut / button → client.py → local Unix socket → GTK app
                                               │
Microphone (parec, 24 kHz PCM) ────────────────┤
                                               ▼
                              Azure realtime transcription
                                               │ partial + final text
                                               ▼
                               Caret target or editable Buffer
                                               │
                                               ▼
                              Local audio + transcript history
```

## The desktop process

`app.py` owns the GTK 3 popup and a user-only Unix socket under `XDG_RUNTIME_DIR`. `client.py` sends commands to that process, so the shortcut does not create duplicate recorders. The popup uses XWayland's always-on-top window state and can be dragged by its title area.

GTK widgets are updated on the GTK thread. Microphone capture and the Azure WebSocket run in a worker thread; incoming transcription events are sent back to GTK through `GLib.idle_add`. The audio meter observes the same PCM chunks and does not modify the samples sent to Azure or written to history.

## Two destinations

**Caret** streams recognized deltas to the original focused field. The app first tries AT-SPI's editable text interface. For fields that do not expose one, it can fall back to `ydotool` with an optional per-user keyboard map. If the target cannot be confirmed or typed into, the app reports that and still saves the transcript.

**Buffer** appends deltas to the GTK text editor. You can edit the result and copy it from the icon in the editor. Switching destinations during a session affects new text only; text already delivered stays in its earlier destination. Stopping in Buffer mode leaves the popup open, while stopping in Caret mode hides it.

An optional GNOME Shell [focus helper](../extension/) remembers and restores the original app window for Caret mode. The shell extension does not bind the recording shortcut; your desktop's custom keyboard shortcut launches `client.py toggle`.

## What stays on disk

Runtime data lives in `~/.local/share/live-dictation/`, outside the Git clone:

| Path | Purpose |
| :-- | :-- |
| `settings.json` | Last selected destination |
| `buffer-draft.txt` | Editable buffer between restarts |
| `history/<timestamp-id>/audio.wav` | Recorded microphone audio |
| `history/<timestamp-id>/transcript.txt` | Exact finalized transcript |
| `history/<timestamp-id>/events.jsonl` | Streaming deltas and session events |
| `history/<timestamp-id>/metadata.json` | Session time, endpoint, model, and result |

History is retained until you remove it. The data directory and files are created with user-only permissions. The API key is read from the process environment or a private file named in `~/.config/live-dictation/config.json`; its value is not written to history.

Audio is sent to your configured Azure OpenAI resource for transcription. Because partial words are delivered immediately, a later model revision may differ from text already inserted at the caret or buffer. The finalized wording is preserved in `transcript.txt`.
