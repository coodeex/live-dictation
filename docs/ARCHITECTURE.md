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

The yellow-hover **×** closes the popup without stopping the background service, so F4 can open it again. If recording is in progress, closing the popup cancels that recording first.

GTK widgets are updated on the GTK thread. Microphone capture runs in a worker thread and writes a local WAV while the audio meter observes the same PCM chunks. F4 stops recording and transcribes; silence does not stop it. The Cancel button appears only during recording and stops capture without uploading or inserting text. The popup stays open and returns to Ready after cancellation; cancelled audio remains in History. After F4, the worker sends the completed file to Azure's audio transcription endpoint and sends the final transcript back to GTK through `GLib.idle_add`.

## Two destinations

**Caret** pastes the completed transcript into the original focused field by default. At F4, the app snapshots the current text clipboard while Azure transcribes. It temporarily owns the clipboard, verifies that `wl-paste` sees the transcript on the Wayland side of XWayland, sends one paste shortcut through `ydotool`, then restores the old text if the clipboard still contains the dictation. The shortcut is sent asynchronously so GTK can serve clipboard requests. If verification fails before any shortcut is sent, it uses the original AT-SPI/`ydotool type` method. Rich clipboard formats may be lost. The original typing method is also available through `client.py type`; `client.py paste` switches back. If target focus is lost or a paste attempt fails, the transcript is placed in Buffer and the popup stays open for copying.

The **✦ Format** toggle persists between sessions. Saying **format** as the final word also formats that one dictation. After transcription, the app sends the cue-free text to the configured Azure chat deployment with a short instruction to improve clarity and grammar while keeping the speaker's wording and meaning. A formatted dictation never presses Enter automatically, including when the **↵** toggle or a spoken send cue is present. If formatting fails, the original wording is delivered without Enter and the popup stays open with an error. History keeps the raw Azure result in `transcript.txt` and the revised result, when available, in `formatted.txt`.

Before displaying or delivering text, a literal `+` between two ASCII letters is changed to a space (for example, `speech+to+text` becomes `speech to text`). The raw Azure transcript remains unchanged in `transcript.txt` for diagnosis. This text cleanup is separate from the `ydotool` keymap, which corrects characters produced by a non-US keyboard layout during fallback typing.

The hardcoded `WORD_CORRECTIONS` map in `app.py` also replaces whole-word recognition mistakes before delivery and in the History viewer. It currently maps `Klarvo` to `Clarvo`, regardless of case. Add another misspelling and preferred spelling there when a repeated error is confirmed. Raw transcripts on disk stay untouched.

The small **↵** toggle is off by default. In Caret mode, enabling it presses the Enter key after successful text insertion, provided the original target still has focus. The target application decides whether Enter sends the message or inserts a line break. The setting persists between sessions and has no effect in Buffer mode.

In Caret mode, ending a dictation with **YOLO** or **boom** also presses Enter after successful insertion, even if **↵** is off. Case and trailing punctuation do not matter. The cue is removed from the text delivered to the target (or Buffer if delivery falls back), while the exact Azure transcript stays in History. Buffer mode does not press Enter.

When **↵** is on, a final **hold** overrides it: the app inserts the text without pressing Enter and removes the spoken cue. With **↵** off, “hold” stays ordinary dictated text. A dictation containing only “hold” leaves the current draft untouched.

The **♫** toggle is also off by default and persists between sessions. When recording starts, it pauses MPRIS players that are currently playing and remembers which player instances it paused. It resumes only those same instances after microphone capture stops, including when the recording is cancelled or capture fails. Turning the toggle on during recording pauses music then; turning it off resumes music it paused.

**Buffer** appends the completed transcript to the GTK text editor. You can edit the result, copy it from the icon in the editor, or clear it after confirmation. The destination selected when transcription finishes receives the text. The popup shows recording time beside the waveform. After F4, it shows transcription time and then delivery time as a third number. Caret mode keeps the popup open through delivery and shows all three final times for three seconds before closing; Buffer mode leaves them visible for review and copying.

The **History** button expands the popup into a transcript browser. It shows ten sessions per page, newest first, with each session's start time, recording duration, transcription duration, delivery duration, and the first 100 characters of the transcript. Each card can expand to show the full text or copy it; **Back** returns to dictation. Older sessions show the previously recorded processing duration instead of the new split timings. History is available while dictation is idle.

An optional GNOME Shell [focus helper](../extension/) remembers and restores the original app window for Caret mode. The shell extension does not bind the recording shortcut; your desktop's custom keyboard shortcut launches `client.py toggle`.

## What stays on disk

Runtime data lives in `~/.local/share/live-dictation/`, outside the Git clone:

| Path | Purpose |
| :-- | :-- |
| `settings.json` | Last selected destination and Enter toggle |
| `buffer-draft.txt` | Editable buffer between restarts |
| `timings.jsonl` | Append-only recording, transcription, optional formatting, Azure request, delivery, paste, AT-SPI, and typing fallback durations, plus delivery method and result |
| `history/<timestamp-id>/audio.wav` | Recorded microphone audio |
| `history/<timestamp-id>/transcript.txt` | Exact finalized transcript |
| `history/<timestamp-id>/formatted.txt` | Revised text when Format succeeded |
| `history/<timestamp-id>/events.jsonl` | Session and final transcription events |
| `history/<timestamp-id>/metadata.json` | Session time, endpoint, model, and result |

History is retained until you remove it. The data directory and files are created with user-only permissions. The API key is read from the process environment or a private file named in `~/.config/live-dictation/config.json`; its value is not written to history.

Audio is sent to your configured Azure OpenAI resource only after recording stops. The file endpoint accepts up to 25 MB. For longer WAV recordings, the app creates a temporary compressed MP3 for upload while keeping the original WAV in History. The finalized wording is preserved in `transcript.txt`.
