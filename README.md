# 8BitDo Retro Keyboard volume knob

Makes the volume knob on the 8BitDo Retro Mechanical Keyboard work on Windows when the normal Windows volume keys don't, for example with a SteelSeries Arctis Nova Pro base station. The program is called **8BitDo Volume Knob**.

The knob changes the volume of the app that is playing:

| App | How it is changed |
|---|---|
| Spotify | [Spotify's own slider](#spotifys-own-slider), after a one-time setup (Spotify Premium). Without it: Windows' own per-app volume |
| Plex (the desktop app) | Windows' own per-app volume: Plex's level in the Windows Volume Mixer, not Plex's own slider |
| Other apps you list | Windows' own per-app volume (set `apps` in the [settings](#settings) file). Plexamp and Plex HTPC are not on by default, but show with their names when you add `plexamp.exe` or `Plex HTPC.exe` |
| YouTube and YouTube Music in Brave | A small Brave extension that moves the player's volume slider |

If nothing is playing, the knob changes all of them. The mute key mutes and unmutes them together. Press it again to unmute what it muted: that app stays the target while it is muted, until something else plays. The same goes for an app you turned down to 0 %.

While the program runs, it takes over the volume keys: Windows' own master volume and Windows' volume pop-up do not react to the knob. The program shows its own [volume pop-up](#volume-pop-up) and a [taskbar icon](#taskbar-icon) instead.

## Volume pop-up

When you turn the knob, a small pop-up shows at the bottom of the screen, like the one Windows has. It shows the app's name (Spotify, Plex, YouTube or YouTube Music; one block each when the knob changes more than one), the volume in %, and a bar. Above 100 % the bar turns orange, with a mark at 100 %. A muted app shows "Muted". When Spotify is full and you turn up, the pop-up shows "Max".

- It goes away 1.5 seconds after the last turn.
- It appears on your main screen. To show it on the screen where the mouse is, set `osd_monitor` to `"mouse"`.
- It never takes focus, and clicks go through it.
- It follows Windows' dark or light mode.
- It is not shown over games in exclusive full screen.

You can turn it off, change how long it stays or which screen it uses in the [settings](#settings) (`osd`, `osd_seconds`, `osd_monitor`).

### YouTube's own bubble

On YouTube, from 0 to 100 %, the knob shows YouTube's own volume bubble (the same one its arrow keys show, with "Muted" too). This is YouTube's own volume: its slider moves and it is remembered. Above 100 % a small box in YouTube's style shows the boost (for example "135%") with a thin red bar and "BOOST". If YouTube's bubble isn't available (for example in an embedded video), the small box shows the volume instead. YouTube Music has no bubble of its own, so the small box shows there. Nothing extra shows when you use YouTube's own controls.

## Taskbar icon

The program puts a knob icon on the taskbar, in the notification area by the clock. Its pointer turns with the volume, and a small number on a dark strip along the bottom shows the volume in %. It is orange when the volume is boosted above 100 %, and grey when muted or paused (no number while paused). For the plain knob without the number, set `tray_style` to `"knob"`. Hover over it to see the exact volume, for example `8BitDo Volume Knob - Spotify 45%`.

Right-click the icon for this menu. It is dark in Windows dark mode and light in light mode. Use the mouse or the keyboard (arrows, Enter, Esc). It closes when you click elsewhere:

| Item | What it does |
|---|---|
| 25% 50% 75% 100% | Sets the volume of what the knob would change (the playing app, else all of them). The volume pop-up shows the result. The closest button is outlined, in orange when the volume is boosted above 100 %. |
| Speaker button (end of that row) | Mutes or unmutes, like the knob's Mute key. When everything is muted it shows a crossed-out speaker with an outline. |
| Pause / Resume | Pause gives the volume keys back to Windows until you press Resume or the program starts again. |
| Open log | Opens the log in Notepad. |
| Settings | Opens the [settings window](#settings). |
| Quit | Stops the program. |

On Windows 11, a new icon may first be hidden under the ^ arrow by the clock. Drag it onto the taskbar to keep it in view. You can hide the icon with the `tray_icon` setting.

## Spotify's own slider

This needs Spotify Premium. Do the setup once. After that the knob moves the volume slider inside Spotify, in the same 1.3 % steps, up to 100 %. Fast turning stays smooth. If Spotify briefly asks the program to slow down, the knob waits instead of switching to Windows' volume. Spotify's volume shown in the pop-up and on the taskbar icon is Spotify's own.

Spotify changes its dashboard now and then, so a button or field name may differ a little.

Setup:

1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard), log in with your Spotify account and accept the Developer Terms if asked.
2. Click **Create app**.
3. Fill in App name `8BitDo Volume Knob`, App description `Volume knob on my PC`, leave Website empty, and Redirect URI `http://127.0.0.1:38480/callback`. Type the Redirect URI exactly like this (`127.0.0.1`, not `localhost`), or the login fails. Click **Add** next to it.
4. Tick **Web API** only, tick the terms box, click **Save**.
5. Open the app's **Settings** and copy the **Client ID** (32 letters and digits). You don't need the Client secret.
6. Open Spotify on this PC. Open Terminal (right-click the Start button, **Terminal**), paste this line and press Enter:

   ```
   py -I "C:\Program Files\8BitDo Volume Knob\volume_knob.py" --spotify-login
   ```

   Paste the Client ID when asked and press Enter.
7. Your browser opens a Spotify page. Click **Agree** within 2 minutes. The page says "Spotify connected", and you can go back to the terminal. It says `Spotify calls this PC "<name>"`. The running knob uses it within a few seconds, with no restart.

If the login doesn't finish, the terminal says why:

| Message says | What to do |
|---|---|
| no answer within 2 minutes | Run the command again and click **Agree** sooner. |
| check the Client ID and Redirect URI | Fix them in your Spotify app (step 3 and 5), then run the command again. |
| another program is using what the login needs | Restart the PC and run the command again. |

A tab that says "Old login page" is from an earlier try. Use the newest Spotify tab.

Things to know:

- It works only while Spotify plays on this PC. If Spotify plays on another device (your phone), or you are not logged in, or there is no internet, or Spotify refuses, the knob uses Windows' per-app volume as before.
- Spotify also has a level in the Windows mixer. When Spotify's own slider is full and that mixer level is lower, turning up raises the mixer level too. "Max" shows only when both are full.
- If you reinstall Spotify, the knob finds this PC's Spotify again by itself.
- Mute is still Windows' mute.
- To go back to Windows' per-app volume only, set `spotify_control` to `"mixer"`.
- To remove the login, run the same command with `--spotify-logout`. You can also remove the app at spotify.com under Account, Apps.

## Louder than 100 % on YouTube

YouTube and YouTube Music can go up to 150 %. The knob always stops once on exactly 100 %, then goes on in the same 1.3 % steps (about 2 full turns from 0 to 100 %). Past 100 % the extension boosts the sound itself and limits it so it doesn't distort. Spotify stays at 100 % at most.

Things to know:

- The boost is not remembered. After a reload, the tab is back at 100 %.
- It only works in a tab you have clicked in at least once (a browser rule). Otherwise that tab stays at 100 %.
- If a boosted tab goes silent (for example after sleep or after switching headphones), the extension tries to bring the sound back by itself. If it can't, the knob shows "Reload tab": reload that tab.
- If another audio extension (for example an equalizer) already handles the video's sound, there is no boost in that tab.

## Safety reset

A YouTube or YouTube Music tab that is louder than 100 % is set to 50 % when:

- the program starts, or
- you open a tab while Spotify and all other YouTube tabs are closed.

This keeps a boosted tab from blasting you when you start listening. Tabs at 100 % or less, mute, and the Windows volume are never changed. You can turn this off in the settings (`failsafe`).

## Install

You need:

- Windows and the Brave browser (Brave only for YouTube).
- Python 3.8 or newer from [python.org](https://www.python.org/downloads/windows/) (tested with 3.13). No extra packages. The Python from the Microsoft Store does not work. Setup tells you if your Python is too old.

The commands below use `py`, the Python launcher that comes with python.org Python. If Python is on your PATH, `python` works too.

Steps:

1. Download `8bitdo-volume-knob-<version>.zip` from the [Releases page](https://github.com/gettic/8bitdo-retro-keyboard-volume-knob/releases). Unzip it to a folder on this PC, not a network drive.
2. Open the `8BitDo Volume Knob` folder from the zip. Right-click an empty spot and choose **Open in Terminal**. No "Open in Terminal" (Windows 10)? Click the folder's address bar, type `cmd` and press Enter.
3. Run:

   ```
   py -I volume_knob.py --install
   ```

   Windows asks once in its permission box. The box names Python (Python Software Foundation). Say yes. Install takes about half a minute (it checks that all files arrived).
4. Load the extension once:
   1. Open `brave://extensions` in Brave.
   2. Turn on **Developer mode**.
   3. Click **Load unpacked** and pick `C:\Program Files\8BitDo Volume Knob\extension`. It shows as **8BitDo Volume Knob**.
5. Reload any YouTube or YouTube Music tabs that were already open.

Step 3 copies the program to `C:\Program Files\8BitDo Volume Knob`, starts it, and makes it start with Windows. Spotify works right away through Windows' per-app volume; [Spotify's own slider](#spotifys-own-slider) is an optional extra setup. Steps 4 and 5 are only for YouTube. After that you can delete the downloaded folder.

If you upgrade Python and remove the old one, the knob stops starting with Windows. Run this once (no permission box). You don't need the downloaded folder for that:

```
py -I "C:\Program Files\8BitDo Volume Knob\volume_knob.py" --install
```

Coming from version 1.0 (the `.exe` version)? First run `Uninstall.cmd` in your old `8BitDo Volume Knob` folder, remove the old extension in `brave://extensions`, then install this version.

### Start with Windows

By default, install puts a shortcut `8BitDo Volume Knob` in your Startup folder. To use a registry startup entry instead, add `--run-value`:

```
py -I volume_knob.py --install --run-value
```

### What the extension does

It runs only on youtube.com and music.youtube.com. It asks for no browser permissions. It talks only to the program on your PC and never sends page titles or addresses.

### How the extension reaches the program

The program listens on your own PC only (`127.0.0.1`, port 38475; if that is taken, 38476 or 38477). Nothing from outside can reach it. Only this extension, in Brave, under your own Windows account, can talk to it. The extension also takes at most one knob step per command and never sets a tab above 100 % by itself, so another program pretending to be the knob can't make a tab loud in one go. After the program starts or restarts, YouTube follows the knob again within a few seconds. If all three ports are taken, the knob still works, but on YouTube it changes Brave's whole sound.

### What install changes

- The folder `C:\Program Files\8BitDo Volume Knob`: the program and the extension. This is the only part that needs the permission box. While setup runs, it uses `C:\Program Files\8BitDo Volume Knob.new` and `.old` next to it; both are gone afterwards.
- The shortcut `8BitDo Volume Knob` in your Startup folder. With `--run-value`: the startup entry `8BitDo Volume Knob` in `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` instead.
- The log folder `%LOCALAPPDATA%\8BitDo Volume Knob`.
- The settings folder `%APPDATA%\8BitDo Volume Knob`.

It also removes the browser registry keys that version 1.0 made. Uninstall removes all of it, including any `.new` or `.old` leftovers.

## Privacy

Nothing leaves your PC, except talking to Spotify if you did the [Spotify setup](#spotifys-own-slider): then the program asks Spotify for the volume and sets it. The login is saved in `%APPDATA%\8BitDo Volume Knob\spotify.json` (the login token is encrypted for your Windows user). The extension sends nothing over the internet. It tells the program only each tab's volume, mute and whether it is playing. It never sends page titles or addresses.

## Update

1. Download and unzip the new version.
2. In its `8BitDo Volume Knob` folder, run `py -I volume_knob.py --install` again (one permission box).
3. In `brave://extensions`, press the reload arrow on the extension. Then reload YouTube and YouTube Music tabs that were already open (or restart Brave). You don't need to remove or add the extension again.

Your settings are kept. A failed update leaves the installed version as it was. If setup says a file is in use, close Brave and run it again.

## Uninstall

1. In a terminal, run:

   ```
   py -I "C:\Program Files\8BitDo Volume Knob\volume_knob.py" --uninstall
   ```

   Windows asks once in its permission box. It stops the program, removes it from Windows startup, and moves the program, settings and log folders to the Recycle Bin.
2. Remove **8BitDo Volume Knob** in `brave://extensions`.
3. If you used the Spotify login, also remove the app at spotify.com under Account, Apps.

## Settings

Right-click the taskbar icon and choose **Settings**. A window opens (dark or light, like Windows). Changes are saved and used at once, with no restart. It sets:

| In the window | Setting |
|---|---|
| Each knob click changes the volume by | `step` |
| Spotify volume: Spotify's own / Windows mixer | `spotify_control` |
| Without the extension, change all of Brave's sound | `brave_fallback` |
| Can go louder than 100%: No / 125% / 150% | `boost_max` |
| Turn tabs over 100% down when they open, Turn them down to | `failsafe`, `failsafe_level` |
| Show the pop-up when I turn the knob, Pop-up stays for, Show it on | `osd`, `osd_seconds`, `osd_monitor` |
| The icon shows: Knob and number / Knob only | `tray_style` |

Use the mouse, or Tab, arrow keys, Space, Enter and Esc. If a change can't be saved, the window says "Couldn't save. Try again, or see Open log".

The other settings are only in the file `%APPDATA%\8BitDo Volume Knob\settings.json`, which the program creates the first time it starts. Click **Open the settings file (advanced)** at the bottom of the window to open it in Notepad. After editing the file, restart the program:

1. Sign out of Windows and back in. Or:
2. Run `py -I "C:\Program Files\8BitDo Volume Knob\volume_knob.py" --stop`, then
3. run `pyw -I "C:\Program Files\8BitDo Volume Knob\volume_knob.py"` (`pyw` starts it without a window).

If the file has a mistake, the window does not change it. It says "Your settings file can't be read, so changes can't be saved." with **Open the file** and **Reset to defaults**. Reset keeps the old file as `settings.broken.json` and writes the defaults. Later backups are numbered (`settings.broken-2.json`, `settings.broken-3.json`, ...), so an older backup is never overwritten.

All settings:

| Setting | Default | What it does |
|---|---|---|
| `step` | 0.013 | Size of one click: 0.013 is 1.3 %. Range 0.01 to 0.20. |
| `apps` | `["plex.exe", "spotify.exe"]` | Windows apps the knob controls, by exe name. An older settings file that still has the old default (`["spotify.exe"]`) gets `plex.exe` added once. A list you changed is left alone. |
| `boost_max` | 1.5 | Highest YouTube volume (150 %). 1.0 turns the boost off. |
| `failsafe` | true | The safety reset above. |
| `failsafe_level` | 0.5 | Volume a loud tab is reset to. |
| `failsafe_start_window` | 20 | Seconds after start in which all tabs are checked. |
| `brave_fallback` | true | Without the extension, the knob changes Brave's whole sound while Brave plays. It puts that back once the extension connects. |
| `peak_threshold` | 0.001 | How loud counts as "playing". |
| `hold_seconds` | 1.5 | How long an app counts as playing after its last sound. |
| `osd` | true | Show the [volume pop-up](#volume-pop-up) when you turn the knob. |
| `osd_seconds` | 1.5 | How long the pop-up stays after the last turn. Range 0.5 to 10. |
| `osd_monitor` | `"primary"` | Which screen shows the pop-up: `"primary"` = the main screen, `"mouse"` = the screen with the mouse. |
| `tray_icon` | true | Show the [taskbar icon](#taskbar-icon). |
| `tray_style` | `"knob_number"` | `"knob"` = the plain knob without the % number. |
| `spotify_control` | `"web"` | `"web"` = [Spotify's own slider](#spotifys-own-slider) once set up. `"mixer"` = always Windows' per-app volume. |
| `log_level` | INFO | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

A bad value is ignored and the default is used instead.

## Troubleshooting

The program writes `volume_knob.log` to `%LOCALAPPDATA%\8BitDo Volume Knob`. Everything, including the link to Brave, is in that one log. Open it from the taskbar icon's menu: **Open log**.

| Problem | What to try |
|---|---|
| The knob does nothing | Hover over the taskbar icon. "paused (Windows controls the volume)": right-click it and choose **Resume**. "another program has the volume keys (retrying)": close the other volume tool; the knob tries again every 30 s. |
| No taskbar icon | The program isn't running (or `tray_icon` is off). Start it with the `pyw` command in [Settings](#settings), or sign out and back in. |
| Spotify's own slider doesn't move | It needs Premium, Spotify playing on this PC, and the [one-time setup](#spotifys-own-slider). Run `--self-test` and look at its "spotify" line. If the login is old, run `--spotify-login` again. Until then the knob uses Windows' per-app volume. |
| YouTube changes all of Brave's sound | The extension isn't connected. In `brave://extensions`, check that it is on and press its reload arrow, then reload the YouTube tab. |
| Antivirus warns | There is no .exe: the program is Python script files run by your own Python. Download it only from the Releases page. If your antivirus warns anyway, don't turn it off; download the zip again. If it keeps blocking, open an issue on GitHub. |
| Still stuck | Run `--self-test`. Every line should start with `[OK]`. Send the lines that say `[FAIL]` and the end of the log in an issue on GitHub. |

If Windows audio stops for a moment (audio service restart, driver update, after sleep), the knob picks it up again by itself. YouTube keeps working meanwhile.

Commands (in a terminal, after the program path), for example:

```
py -I "C:\Program Files\8BitDo Volume Knob\volume_knob.py" --self-test
```

| Command | What it does |
|---|---|
| `--stop` | Stops the running program. |
| `--self-test` | Checks that everything works, without changing anything. |
| `--spotify-login` | The one-time [Spotify setup](#spotifys-own-slider) login. |
| `--spotify-logout` | Removes the Spotify login. |
| `--version` | Shows the version. |
| `--help` | Lists all options, including `--install` and `--uninstall`. |

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

Run the tests from `code\` (the full suite takes about 30 s):

```
python -m unittest discover -s tests -t .
python -E -s -m unittest setup_knob.test_setup
python -E -s -m unittest setup_knob.test_release
```

The extension's tests run headless with Node, no packages: `node --test tests/extension_js/`. The first command above runs them too when Node is installed.

Build the release zip (no packages needed, but git is). Commit first: the build stops if a shipped file isn't committed.

```
python -I make_release.py --version <version>
```

## License

GPL-3.0, see LICENSE.
