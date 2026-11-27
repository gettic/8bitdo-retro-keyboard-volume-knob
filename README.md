# 8BitDo Retro Keyboard volume knob

Makes the volume knob on the 8BitDo Retro Mechanical Keyboard work on Windows when the normal Windows volume keys don't, for example with a SteelSeries Arctis Nova Pro base station.

The knob changes the volume of the app that is playing:

| App | How it is changed |
|---|---|
| Spotify | Spotify's own volume bar, after a [one-time setup](#spotifys-own-volume-bar) (Spotify Premium). Without it: Windows' own per-app volume |
| Other apps you list | Windows' own per-app volume |
| YouTube and YouTube Music in Brave | A small Brave extension that moves the player's volume slider |

If nothing is playing, the knob changes all of them. The mute key mutes and unmutes them together.

While the program runs, it takes over the volume keys: Windows' own master volume and the volume pop-up do not react to the knob. The program shows its own [volume card](#volume-card) and a [tray icon](#tray-icon) instead.

## Volume card

When you turn the knob, a small card shows at the bottom of the screen, like the one Windows has. It shows the app's name (Spotify, YouTube or YouTube Music; one block each when the knob changes more than one), the volume in %, and a bar. Above 100 % the bar turns orange, with a mark at 100 %. A muted app shows "Muted".

- It goes away 1.5 seconds after the last turn.
- It appears on your main screen. To show it on the screen where the mouse is, set `osd_monitor` to `"mouse"`.
- It never takes focus, and clicks go through it.
- It follows Windows' dark or light mode.
- It is not shown over games in exclusive full screen.

You can turn it off, change how long it stays or which screen it uses in the [settings](#settings) (`osd`, `osd_seconds`, `osd_monitor`).

### YouTube's own bubble

On YouTube, from 0 to 100 %, the knob shows YouTube's own volume bubble (the same one its arrow keys show, with "Muted" too). This is YouTube's own volume: its slider moves and it is remembered. Above 100 % a small box in YouTube's style shows the boost (for example "135%") with a thin red bar and "BOOST". If YouTube's bubble isn't available (for example in an embedded video), the small box shows the volume instead. YouTube Music has no bubble of its own, so the small box shows there. Nothing extra shows when you use YouTube's own controls.

## Tray icon

The program puts a knob icon in the notification area by the clock. Its pointer turns with the volume, and a small number on a dark strip along the bottom shows the volume in %. It is orange when the volume is boosted above 100 %, and grey when muted or paused (no number while paused). For the plain knob without the number, set `tray_style` to `"knob"`. Hover over it to see the exact volume, for example `8BitDo Volume Knob - Spotify 45%`.

Right-click the icon for this menu. It is dark in Windows dark mode and light in light mode. Use the mouse or the keyboard (arrows, Enter, Esc). It closes when you click elsewhere:

| Item | What it does |
|---|---|
| 25% 50% 75% 100% | Sets the volume of what the knob would change (the playing app, else all of them). The volume card shows the result. The closest button is outlined, in orange when the volume is boosted above 100 %. |
| Speaker button (end of that row) | Mutes or unmutes, like the knob's Mute key. When everything is muted it shows a crossed-out speaker with an outline. |
| Pause / Resume | Pause gives the volume keys back to Windows until you press Resume or the program starts again. |
| Open log | Opens the log in Notepad. |
| Settings | Opens the [settings window](#settings). |
| Quit | Stops the program. |

On Windows 11, a new icon may first be hidden under the ^ arrow by the clock. Drag it onto the taskbar to keep it in view. You can hide the icon with the `tray_icon` setting.

## Spotify's own volume bar

This needs Spotify Premium. Do the setup once. After that the knob moves the volume bar inside Spotify, in the same 1.3 % steps, up to 100 %. Spotify's volume shown in the card and tray icon is Spotify's own.

Setup:

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard), log in with your Spotify account and accept the Developer Terms if asked.
2. Click **Create app**.
3. Fill in App name `8BitDo volume knob`, App description `Volume knob on my PC`, leave Website empty, and Redirect URI `http://127.0.0.1:38480/callback`. Click **Add** next to the Redirect URI.
4. Tick **Web API** only, tick the terms box, click **Save**.
5. Open the app's **Settings** and copy the **Client ID**. You don't need the Client secret.
6. With Spotify open on this PC, run this in a terminal and paste the Client ID when asked:

   ```
   py -I "C:\Program Files\8BitDo Volume Knob\volume_knob.py" --spotify-login
   ```
7. Your browser opens a Spotify page. Click **Agree**. The page says "Spotify connected". The running knob uses it within a few seconds, with no restart.

Spotify changes its dashboard now and then, so a field name may differ a little.

Things to know:

- It works only while Spotify plays on this PC. If Spotify plays on another device (your phone), or you are not logged in, or there is no internet, or Spotify refuses, the knob uses Windows' per-app volume as before.
- Mute is still Windows' mute.
- To go back to Windows' per-app volume only, set `spotify_control` to `"mixer"`.
- To remove the login, run the same command with `--spotify-logout`. You can also remove the app at spotify.com under Account, Apps.

## Louder than 100 % on YouTube

YouTube and YouTube Music can go up to 150 %. The knob always stops once on exactly 100 %, then goes on in the same 1.3 % steps (about 2 full turns from 0 to 100 %). Past 100 % the extension boosts the sound itself and limits it so it doesn't distort. Spotify stays at 100 % at most.

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

You need:

- Windows and the Brave browser (Brave only for YouTube).
- Python 3 from [python.org](https://www.python.org/downloads/windows/) (tested with 3.13). No extra packages. The Python from the Microsoft Store does not work.

The commands below use `py`, the Python launcher that comes with python.org Python. If Python is on your PATH, `python` works too.

Steps:

1. Download `8bitdo-volume-knob-<version>.zip` from the [Releases page](https://github.com/gettic/8bitdo-retro-keyboard-volume-knob/releases) and unzip it.
2. Open the `8BitDo Volume Knob` folder from the zip. Right-click an empty spot and choose **Open in Terminal**.
3. Run:

   ```
   py -I volume_knob.py --install
   ```

   Windows asks once for admin rights. The box names Python (Python Software Foundation). Say yes.
4. Load the extension once:
   1. Open `brave://extensions` in Brave.
   2. Turn on **Developer mode**.
   3. Click **Load unpacked** and pick `C:\Program Files\8BitDo Volume Knob\extension`.
5. Reload any YouTube or YouTube Music tabs that were already open.

Step 3 copies the program to `C:\Program Files\8BitDo Volume Knob`, starts it, and makes it start with Windows. Spotify works right away. Steps 4 and 5 are only for YouTube. After that you can delete the downloaded folder.

If you upgrade Python later and it moves to a new folder, run step 3 again.

Coming from version 1.0 (the `.exe` version)? First run `Uninstall.cmd` in your old `8BitDo Volume Knob` folder, remove the old extension in `brave://extensions`, then install this version.

### Start with Windows

By default, install puts a shortcut `8BitDo Volume Knob` in your Startup folder. To use a registry startup entry instead, add `--run-value`:

```
py -I volume_knob.py --install --run-value
```

### What the extension does

It runs only on youtube.com and music.youtube.com. It asks for no browser permissions. It talks only to the program on your PC and never sends page titles or addresses.

### How the extension reaches the program

The program listens on your own PC only (`127.0.0.1`, port 38475; if that is taken, 38476 or 38477). Nothing from outside can reach it. It accepts only this extension, running in Brave under your own Windows user. If all three ports are taken, the knob still works, but on YouTube it changes Brave's whole sound.

### What install changes

- The folder `C:\Program Files\8BitDo Volume Knob`: the program and the extension. This is the only part that needs admin rights.
- The shortcut `8BitDo Volume Knob` in your Startup folder. With `--run-value`: the startup entry `8BitDo Volume Knob` in `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` instead.
- The log folder `%LOCALAPPDATA%\8BitDo Volume Knob`.
- The settings folder `%APPDATA%\8BitDo Volume Knob`.

It also removes the browser registry keys that version 1.0 made. Uninstall removes all of it.

## Privacy

Nothing leaves your PC, except talking to Spotify if you did the [Spotify setup](#spotifys-own-volume-bar): then the program asks Spotify for the volume and sets it. The login is saved in `%APPDATA%\8BitDo Volume Knob\spotify.json` (the login token is encrypted for your Windows user). The extension sends nothing over the internet. It tells the program only each tab's volume, mute and whether it is playing. It never sends page titles or addresses.

## Update

1. Download and unzip the new version.
2. In its `8BitDo Volume Knob` folder, run `py -I volume_knob.py --install` again (one admin box).
3. In `brave://extensions`, press the reload arrow on the extension.

Updating keeps the `step` already in your settings. To use the new default, open **Settings** in the tray menu and set "Each knob click changes the volume by" to 1.3 %.

## Uninstall

1. In a terminal, run:

   ```
   py -I "C:\Program Files\8BitDo Volume Knob\volume_knob.py" --uninstall
   ```

   Windows asks once for admin rights. It stops the program, removes it from Windows startup, and moves the program, settings and log folders to the Recycle Bin.
2. Remove the extension in `brave://extensions`.

## Settings

Right-click the tray icon and choose **Settings**. A window opens (dark or light, like Windows). Changes are saved and used at once, with no restart. It sets:

| In the window | Setting |
|---|---|
| Each knob click changes the volume by | `step` |
| Spotify volume: Spotify's own / Windows mixer | `spotify_control` |
| No Brave extension: change all of Brave's sound | `brave_fallback` |
| Can go louder than 100 %: No / 125 % / 150 % | `boost_max` |
| Turn tabs over 100 % down when they open, and to what level | `failsafe`, `failsafe_level` |
| Show the pop-up, how long it stays, which screen | `osd`, `osd_seconds`, `osd_monitor` |
| The icon shows: knob and number / knob only | `tray_style` |

Use the mouse, or Tab, arrow keys, Space, Enter and Esc.

The other settings are only in the file `%APPDATA%\8BitDo Volume Knob\settings.json`, which the program creates the first time it starts. Click **Open the settings file (advanced)** at the bottom of the window to open it in Notepad. After editing the file, restart the program: sign out of Windows and back in, or use `--stop` (see [Troubleshooting](#troubleshooting)) and then run `pyw -I "C:\Program Files\8BitDo Volume Knob\volume_knob.py"` (`pyw` starts it without a window).

If the file has a mistake, the window does not change it. It shows a notice with **Open the file** and **Reset to defaults**. Reset keeps the old file as `settings.broken.json` and writes the defaults. Later backups are numbered (`settings.broken-2.json`, `settings.broken-3.json`, ...), so an older backup is never overwritten.

All settings:

| Setting | Default | What it does |
|---|---|---|
| `step` | 0.013 | Size of one click: 0.013 is 1.3 %. Range 0.01 to 0.20. |
| `apps` | `["spotify.exe"]` | Windows apps the knob controls, by exe name. |
| `boost_max` | 1.5 | Highest YouTube volume (150 %). 1.0 turns the boost off. |
| `failsafe` | true | The safety reset above. |
| `failsafe_level` | 0.5 | Volume a loud tab is reset to. |
| `failsafe_start_window` | 20 | Seconds after start in which all tabs are checked. |
| `brave_fallback` | true | Without the extension, the knob changes Brave's whole sound. |
| `peak_threshold` | 0.001 | How loud counts as "playing". |
| `hold_seconds` | 1.5 | How long an app counts as playing after its last sound. |
| `osd` | true | Show the [volume card](#volume-card) when you turn the knob. |
| `osd_seconds` | 1.5 | How long the card stays after the last turn. Range 0.5 to 10. |
| `osd_monitor` | `"primary"` | Which screen shows the card: `"primary"` = the main screen, `"mouse"` = the screen with the mouse. |
| `tray_icon` | true | Show the [tray icon](#tray-icon). |
| `tray_style` | `"knob_number"` | `"knob"` = the plain knob without the % number. |
| `spotify_control` | `"web"` | `"web"` = [Spotify's own volume bar](#spotifys-own-volume-bar) once set up. `"mixer"` = always Windows' per-app volume. |
| `log_level` | INFO | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

A bad value is ignored and the default is used instead.

## Troubleshooting

The program writes `volume_knob.log` to `%LOCALAPPDATA%\8BitDo Volume Knob`. Everything, including the link to Brave, is in that one log.

Commands (run in a terminal, after the program path):

```
py -I "C:\Program Files\8BitDo Volume Knob\volume_knob.py" --self-test
```

| Command | What it does |
|---|---|
| `--stop` | Stops the running program. |
| `--self-test` | Checks that everything works, without changing anything. It includes a "browser link" and a "spotify" line. |
| `--spotify-login` | The one-time [Spotify setup](#spotifys-own-volume-bar) login. |
| `--spotify-logout` | Removes the Spotify login. |
| `--version` | Shows the version. |

## For developers

The code is Python 3 (standard library only, no packages). Run it from the `code` folder, for example `python volume_knob.py --self-test`.

| Path | What it is |
|---|---|
| `volume_knob.py` | Entry point |
| `knob\` | The program: hotkeys, Windows audio, rules, safety reset, local link to Brave |
| `extension\` | The Brave extension |
| `setup_knob\` | `--install` and `--uninstall` |
| `PROTOCOL.md` | How the program and the extension talk |
| `make_release.py` | Builds the release zip into `dist\` |
| `tests\` | Tests |

Run the tests from `code\`:

```
python -m unittest discover -s tests -t .
python -E -s -m unittest setup_knob.test_setup
```

Build the release zip (no packages needed):

```
python -I make_release.py --version 1.5.0
```

## License

GPL-3.0, see LICENSE.
