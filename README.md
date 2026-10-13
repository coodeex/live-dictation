# Live dictation on this laptop

Press **F4** to start or stop Azure live dictation. Handy remains on **F3**.

Stopping with F4 immediately terminates microphone capture. In Buffer mode the popup stays open so the text can be reviewed or copied; in Caret mode it hides. Azure may finish processing the last audio already sent after the microphone is off; that final text is saved in History.

The default destination is Caret: recognized text goes to the original text field. Switch to Buffer at any time to append subsequent words to the editable text area in the popup. Switching changes the destination of future words; text already sent stays where it was. The copy icon in the editor copies its contents to the clipboard and briefly shows a checkmark. The close button minimizes the popup, and History opens the saved sessions folder.

The selected destination and editable buffer draft survive service restarts. The draft is stored in `~/.local/share/live-dictation/buffer-draft.txt` with user-only permissions.

The popup uses the same X11 always-on-top window state as Focus2's Tk timer. Its GTK service runs through XWayland and reissues the topmost request whenever the window is shown, so GNOME retains `_NET_WM_STATE_ABOVE` when another app is clicked.

Every recording is stored under `~/.local/share/live-dictation/history/<timestamp-id>/` with `audio.wav`, `transcript.txt`, `events.jsonl`, and `metadata.json`. The app keeps sessions indefinitely. The exact finalized transcript is in `transcript.txt`; a rare model revision to already streamed words may leave the destination text different, and the popup reports that case.

The app reads only `AZURE_OPENAI_API_KEY` from `~/.codex/.env`. It uses the `gpt-live-transcribe` deployment on `galactic-azure-openai`. The key and request headers are not copied into session history. Credentials, recordings, transcripts, and the editable draft are kept outside this repository.

Caret insertion prefers AT-SPI editable text, which supports Unicode and leaves the clipboard alone. Other fields use the existing passwordless `ydotool` setup, with mappings for this laptop's Finnish keyboard. If a field does not accept either method, the popup reports the issue and History still keeps the audio and transcript.

The user service is `live-dictation.service`; it is enabled at login. GNOME's custom F4 shortcut calls `client.py toggle`. Other local commands are `client.py show`, `client.py status`, `client.py buffer`, and `client.py cursor` (the internal command name for Caret mode). The F4 shortcut and Handy F3 shortcut are separate.

`live-dictation@juha.local` is installed as a GNOME Shell focus helper and enabled for the next login. GNOME 42 on Wayland does not load a newly installed extension into an existing session. Until then, the app uses AT-SPI to restore the original field after showing the popup. If focus cannot be confirmed, it stops typing and saves the transcript and audio in History.

Service logs: `journalctl --user -u live-dictation.service -n 50 --no-pager`. Verify the service with `systemctl --user status live-dictation.service`. To stop the app without changing Handy: `systemctl --user stop live-dictation.service`.
