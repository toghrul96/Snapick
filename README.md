# Snapick

A screenshot tool for GNOME Wayland that lets you select, annotate, and save exactly what you need and nothing more.
Single Python file, no daemon, no tray icon. Bind it to a key and it's out of the way until you need it.

---

## What it does

- Drag to select any region of the screen
- Resize or move the selection after drawing it
- Annotate with a freehand pen (draw, erase, undo)
- Copy to clipboard, save to a default folder, or pick a folder on the fly
- Desktop notification on save/copy
- Remembers your last save folder between sessions

---

## Requirements

**Python packages:**
```
pip install pygame pillow
```

**System packages (Ubuntu/Debian):**
```
sudo apt install python3-dbus python3-gi gir1.2-glib-2.0 wl-clipboard zenity libnotify-bin
```

Screen capture goes through the XDG Desktop Portal over D-Bus - the standard Wayland approach, no extra permissions needed.

---

## Usage

```bash
python3 snapick.py
```

For everyday use, bind it to a key in GNOME:

**Settings -> Keyboard -> View and Customize Shortcuts -> Custom Shortcuts -> +**

- Name: `Snapick`
- Command: `python3 /path/to/snapick.py`
- Shortcut: `Print` (or whatever you prefer)

### Controls

| Action | How |
|--------|-----|
| Select region | Click and drag |
| Move selection | Drag inside the box |
| Resize selection | Drag any of the 8 handles |
| Draw on screenshot | Click **Draw**, then freehand on the selection |
| Erase | Click **Erase** |
| Undo last stroke | Click **Undo** |
| Copy to clipboard | Click **Copy** |
| Save to default folder | Click **Save** or `Ctrl+S` |
| Save to a different folder | Click **Save As** or `Ctrl+Shift+S` |
| Exit draw/erase mode | `Esc` (keeps the selection) |
| Cancel | `Esc` from any other state, or click **X** |

Screenshots are saved as `Screenshot_1.png`, `Screenshot_2.png`, etc. in `~/Pictures/Screenshots` by default. The folder you pick with Save As is remembered for next time.

---

## Tested on

- Ubuntu 24.04 (GNOME 46, Wayland)
- Ubuntu 26.04 (GNOME 48, Wayland)

Should work on Fedora/Arch with GNOME as well. X11 is untested.
