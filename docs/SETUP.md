# Set up Live Dictation

Live Dictation currently targets a GNOME desktop on Linux. The popup uses GTK 3 through XWayland; the microphone is read through PulseAudio's `parec` command (also provided by PipeWire's PulseAudio compatibility layer). You need an Azure OpenAI resource with a `gpt-transcribe` file transcription deployment.

## 1. Install the app

On Ubuntu, install the GTK introspection bindings and audio tools:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-atspi-2.0 pulseaudio-utils xdg-utils libglib2.0-bin ffmpeg
git clone https://github.com/coodeex/live-dictation.git
cd live-dictation
```

No PyPI packages are required. `ffmpeg` compresses recordings that exceed the file endpoint's upload limit; the original WAV remains in local History. On other distributions, install equivalent GTK 3, AT-SPI, PulseAudio client, and FFmpeg packages.

## 2. Configure Azure privately

Copy the safe example to your user config directory, **outside the repository**:

```bash
mkdir -p ~/.config/live-dictation
chmod 700 ~/.config/live-dictation
cp config.example.json ~/.config/live-dictation/config.json
chmod 600 ~/.config/live-dictation/config.json
${EDITOR:-nano} ~/.config/live-dictation/config.json
```

Set `azure_endpoint` to your Azure OpenAI resource URL and `deployment` to your `gpt-transcribe` deployment name. The example points `api_key_file` at `~/.config/live-dictation/azure.env`. Create that file with user-only permissions and add one line, `AZURE_OPENAI_API_KEY=...`:

```bash
install -m 600 /dev/null ~/.config/live-dictation/azure.env
${EDITOR:-nano} ~/.config/live-dictation/azure.env
```

Do not put the key in `config.example.json` or any file inside the clone. You can instead provide `AZURE_OPENAI_API_KEY` through the environment. Environment variables `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_KEY_FILE`, and `LIVE_DICTATION_CONFIG` override their corresponding config values.

`ydotool_keymap` is optional. Leave it empty for an ordinary US key layout; add character-to-key mappings if your layout needs them for the fallback typing method. The app prefers AT-SPI insertion and only uses this mapping when it falls back to `ydotool`.

## 3. Try it in a terminal

```bash
GDK_BACKEND=x11 python3 app.py
```

In another terminal, run `python3 client.py show`, then `python3 client.py toggle` to start and stop. `python3 client.py status` reports whether the microphone is running. The popup starts in the last selected destination; Caret is the default on a fresh install.

## 4. Run it at login

Create `~/.config/systemd/user/live-dictation.service` with the following unit. Replace both `/ABSOLUTE/PATH/TO/live-dictation` placeholders with your clone's absolute path:

```ini
[Unit]
Description=Live Dictation
After=graphical-session.target
Wants=graphical-session.target

[Service]
Type=simple
Environment=GDK_BACKEND=x11
ExecStart=/usr/bin/python3 /ABSOLUTE/PATH/TO/live-dictation/app.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

Then run:

```bash
systemctl --user daemon-reload
systemctl --user enable --now live-dictation.service
systemctl --user status live-dictation.service
```

In **GNOME Settings → Keyboard → Custom Shortcuts**, add a shortcut for `python3 /ABSOLUTE/PATH/TO/live-dictation/client.py toggle`. Choose F4 or another free key. The shortcut talks to the already-running user service; it does not start a second app process.

## 5. Optional Caret-mode focus helper

The GNOME Shell extension in [`extension/`](../extension/) remembers the app that had focus before dictation and can restore it when you select Caret. To install it on GNOME 42:

```bash
install -d ~/.local/share/gnome-shell/extensions/live-dictation@coodeex
cp extension/extension.js extension/metadata.json ~/.local/share/gnome-shell/extensions/live-dictation@coodeex/
```

Log out and back in, then run `gnome-extensions enable live-dictation@coodeex`. Other GNOME versions may require adapting the extension metadata or API. Without the extension, the app uses AT-SPI focus restoration where possible.

For text fields without an AT-SPI editable interface, Caret mode falls back to `sudo -n ydotool type`. The optional **↵** toggle also uses `sudo -n ydotool key enter` to press Enter after insertion. These paths need `ydotool` and narrowly scoped, passwordless local permission for those commands. Buffer mode does not need them. Configure input injection according to your distribution's security model.

## Useful commands

| Action | Command |
| :-- | :-- |
| Show popup | `python3 client.py show` |
| Toggle recording | `python3 client.py toggle` |
| Inspect state | `python3 client.py status` |
| Select Buffer | `python3 client.py buffer` |
| Select Caret | `python3 client.py cursor` |
| Inspect logs | `journalctl --user -u live-dictation.service -n 50 --no-pager` |

The `cursor` command is the internal name for the UI's **Caret** destination. See [How it works](ARCHITECTURE.md) for the recording, transcription, and history paths.
