# Live Dictation

> **Speak naturally. Press stop. Let the words land.**

Live Dictation is a small, always-on-top voice panel for GNOME on Linux. It records your speech locally, then transcribes it with Azure OpenAI when you stop. The finished text goes into the app you are using or an editable buffer.

![Live Dictation popup above an editor](assets/live-dictation.png)

## One shortcut. Two ways to write.

| **Caret** | **Buffer** |
| :-- | :-- |
| The finished transcript appears at the active text cursor when you stop recording. | The finished transcript goes into an editable panel you can review and copy. |

Switch destinations during a recording. The translucent GTK panel stays above your windows, shows a live audio meter, and keeps every session’s audio and transcript in a local history.

The panel opens at the top right of your primary monitor. Drag it elsewhere while it is open; switching between Caret and Buffer keeps that position.

**[Get started →](docs/SETUP.md)** · **[See how it works →](docs/ARCHITECTURE.md)**

## Made to stay out of the way

- **One complete result.** Speech is transcribed after you stop, then inserted once.
- **Your choice of destination.** Use the active caret for speed or the buffer for control.
- **Quiet when you speak.** Optionally pause playing media during dictation and resume only what the app paused.
- **A record you can revisit.** Audio, transcript, and session events are saved locally.
- **A native desktop feel.** GTK 3, a draggable glass panel, a compact audio meter, and a single shortcut to start or stop.

## Bring your own Azure deployment

Clone the repo, install the Linux dependencies, and point a private user config at your Azure OpenAI `gpt-transcribe` deployment. The app uploads one completed audio file after you stop, so pauses do not end the recording. Credentials and recordings stay outside Git. The [setup guide](docs/SETUP.md) walks through the service, shortcut, and optional GNOME focus helper; [architecture notes](docs/ARCHITECTURE.md) explain the recording and transcription paths.

Built and tested on Ubuntu with GNOME, GTK 3, and XWayland. Audio is sent to your configured Azure OpenAI resource for transcription.
