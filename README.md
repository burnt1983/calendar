# Calendar

A square, frameless month calendar for the Linux desktop. Click a day to add, edit, or delete entries. Each entry has a category, and each category has a colour.

Sync is optional. If GNOME Online Accounts and Evolution Data Server are installed, pick a calendar from the right-click menu and entries are read from and written to that calendar. Without those libraries the widget keeps entries in a local file.

## Install

Needs Python 3, GTK 3, PyGObject, and cairo (`python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-3.0`).

Optional sync: `gir1.2-edataserver-1.2`, `gir1.2-ecal-2.0`, `gir1.2-ical-3.0`, `evolution-data-server`, and `gnome-online-accounts`.

```bash
git clone https://github.com/burnt1983/calendar.git
cd calendar
bash install.sh
calendar-widget
```

Entries are saved in `~/.local/share/calendar-widget/entries.json`. The chosen calendar and window place are saved under `~/.config/calendar-widget/`. It does not start at login. To open it at login, copy the desktop file into `~/.config/autostart/` and set `X-GNOME-Autostart-enabled=true`.

## How to use it

| Action | What happens |
|---|---|
| Click a day | Open that day's entries |
| Left-drag | Move the window |
| Drag an edge | Resize |
| Left / Right | Previous / next month |
| Up / Down | Previous / next year |
| t, or Home | Jump to today |
| o | Transparency on or off |
| [ / ] | Less or more see-through |
| Right-click | Important, today, categories, calendar account, quit |
| Esc, or q | Quit |

Tick Important on a day to draw a red mark on that day. The mark stays in the widget. It is not written to the online calendar.

## Uninstall

```bash
bash uninstall.sh
```

## Licence

MIT. See [LICENSE](LICENSE).
