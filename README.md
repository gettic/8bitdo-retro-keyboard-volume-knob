# 8BitDo Retro Keyboard volume knob

Makes the volume knob on the 8BitDo Retro Mechanical Keyboard work on Windows when the normal Windows volume keys don't, for example with a SteelSeries Arctis Nova Pro base station.

The knob changes the volume of the app that is playing:

| App | How it is changed |
|---|---|
| Spotify (and other apps you list) | Windows' own per-app volume |
| YouTube and YouTube Music in Brave | A small Brave extension that moves the player's volume slider |

If nothing is playing, the knob changes all of them. The mute key mutes and unmutes them together.

While the program runs, it takes over the volume keys: Windows' own master volume and the volume pop-up do not react to the knob.

## Louder than 100 % on YouTube

YouTube and YouTube Music can go up to 150 %. The knob always stops once on exactly 100 %, then goes on in the same 4 % steps. Past 100 % the extension boosts the sound itself and limits it so it doesn't distort. Spotify stays at 100 % at most.

Things to know:

- The boost is not remembered. After a reload, the tab is back at 100 %.
- It only works in a tab you have clicked in at least once (a browser rule). Otherwise that tab stays at 100 %.
- If another audio extension (for example an equalizer) already handles the video's sound, there is no boost in that tab.

## Safety reset

A YouTube or YouTube Music tab that is louder than 100 % is set to 50 % when:

- the program starts, or
- you open a tab while Spotify and all other YouTube tabs are closed.

This keeps a boosted tab from blasting you when you start listening. Tabs at 100 % or less, mute, and the Windows volume are never changed. You can turn this off in the settings (`failsafe`).

## Install

You need Windows and the Brave browser (for YouTube). No admin rights are needed.

1. Download `8bitdo-volume-knob-<version>.zip` from the [Releases page](https://github.com/gettic/8bitdo-retro-keyboard-volume-knob/releases).
2. Unzip the whole `8BitDo Volume Knob` folder somewhere permanent, for example `%LOCALAPPDATA%\Programs`. Don't run it from inside the zip.
3. Double-click `Install.cmd`. It sets everything up, starts the program, and makes it start with Windows.
4. Load the extension once. The setup window tells you the folder (`%LOCALAPPDATA%\8BitDo Volume Knob\extension`):
   1. Open `brave://extensions`.
   2. Turn on **Developer mode**.
   3. Click **Load unpacked** and pick that `extension` folder.
5. Reload any YouTube or YouTube Music tabs that were already open.

Spotify works right after step 3. Steps 4 and 5 are only for YouTube.

The program file is not signed, so Windows (SmartScreen) or your antivirus may warn you the first time. Download it only from the Releases page above.

### What the extension does

It runs only on youtube.com and music.youtube.com. Its only permission is to talk to the program on your PC. It sends no data anywhere else, and never page titles or addresses.

### What Install.cmd changes

Everything is in your own Windows user account. No admin rights are needed.

- The Windows startup entry `8BitDo Volume Knob` (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`).
- The registry key `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.gettic.volume_knob` (Brave reads this one).
- The registry key `HKCU\Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\com.gettic.volume_knob`.
- The folder `%LOCALAPPDATA%\8BitDo Volume Knob`: the connection file, a copy of the extension, and the logs.
- The settings folder `%APPDATA%\8BitDo Volume Knob`.

Uninstall removes all of it.

## Privacy

The program and the extension send nothing over the internet. The extension talks only to the program on your PC, through a local connection that only your Windows user can open. It never reads page titles or addresses.

## Update

1. Close Brave. (It keeps the program file in use.)
2. Unzip the new version over the old folder.
3. Run `Install.cmd` again.

## Uninstall

1. Double-click `Uninstall.cmd`. It stops the program, removes it from Windows startup, and moves its settings and log folders to the Recycle Bin.
2. Remove the extension in `brave://extensions`.
3. Delete the program folder.

## Settings

The program creates `%APPDATA%\8BitDo Volume Knob\settings.json` the first time it starts. Edit it with Notepad, then restart the program (run `volume_knob.exe --stop`, then start it again, or sign out and in).

| Setting | Default | What it does |
|---|---|---|
| `step` | 0.04 | Size of one click: 0.04 is 4 %. Range 0.01 to 0.20. |
| `apps` | `["spotify.exe"]` | Windows apps the knob controls, by exe name. |
| `boost_max` | 1.5 | Highest YouTube volume (150 %). 1.0 turns the boost off. |
| `failsafe` | true | The safety reset above. |
| `failsafe_level` | 0.5 | Volume a loud tab is reset to. |
| `failsafe_start_window` | 20 | Seconds after start in which all tabs are checked. |
| `brave_fallback` | true | Without the extension, the knob changes Brave's whole sound. |
| `peak_threshold` | 0.001 | How loud counts as "playing". |
| `hold_seconds` | 1.5 | How long an app counts as playing after its last sound. |
| `log_level` | INFO | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

A bad value is ignored and the default is used instead.

## Troubleshooting

The program writes `volume_knob.log` to `%LOCALAPPDATA%\8BitDo Volume Knob`. The link between Brave and the program writes `native_host.log` in the same folder.

Commands for `volume_knob.exe` (run from the program folder in a terminal):

| Command | What it does |
|---|---|
| `--stop` | Stops the running program. |
| `--self-test` | Checks that everything works, without changing anything. |
| `--version` | Shows the version. |

## For developers

The code is Python (standard library only; no packages needed to run it). Run it from the `code` folder.

| Path | What it is |
|---|---|
| `volume_knob.py` | Entry point |
| `knob\` | The program: hotkeys, Windows audio, rules, safety reset |
| `native_host\` | The bridge Brave starts to talk to the program |
| `extension\` | The Brave extension |
| `setup_knob\` | `--install` and `--uninstall` |
| `build.py` | Builds the release zip into `dist\` |
| `PROTOCOL.md` | How the program and the bridge talk |
| `tests\` | Tests |

Run the tests from `code\`:

```
python -m unittest discover -s tests -t .
python -m unittest native_host.test_host
python -m unittest setup_knob.test_setup
```

Build the zip (needs the build environment in `build\venv`):

```
python -I build.py --version 1.0.0
```

## License

GPL-3.0, see LICENSE.
