#!/usr/bin/env python3
"""Desktop calendar widget for GNOME (GTK3).

A square, frameless month calendar. Click a day to add, edit or delete entries.
Each entry has a category, and each category has a colour. Day cells are
filled with the colours of their categories.

Sync (optional): if GNOME Online Accounts and Evolution Data Server (EDS) are
installed and a calendar is picked (right-click > Calendar account), entries
are read from and written to that calendar. Without EDS the widget keeps
entries in a local JSON file.

Deps: python3-gi, gir1.2-gtk-3.0
Optional sync deps: gir1.2-edataserver-1.2 gir1.2-ecal-2.0 gir1.2-ical-3.0
                    evolution-data-server gnome-online-accounts

Controls:
  Click a day                 open that day's entries (add / edit / delete)
  Important (day dialog or   red ! across that day
    right-click a day)
  Left / PageUp, arrow <      previous month
  Right / PageDown, arrow >   next month
  Up / Down                   previous / next year
  t / Home / click title      jump to today
  o                           transparency on / off (whole calendar)
  [ / ]                       less / more see-through (20/40/60/80 %)
  Left-drag                   move window (a click that stays still still opens the day)
  Drag edges/corners          resize
  Right-click                 menu (Important on a day, Today, Manage categories,
                              Calendar account, Transparent background, Quit)
  Esc / q                     quit

Files:
  ~/.local/share/calendar-widget/entries.json   entries, categories, sync cache
  ~/.config/calendar-widget/settings.json       calendar account, toggles
  ~/.config/calendar-widget/geometry            window position/size
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

APP_ID = "calendar-widget"
DEFAULT_SIZE = 560
MIN_SIZE = 280
EDGE_GRIP = 10
REFRESH_SECONDS = 60
HOME = Path.home()
CONFIG_DIR = HOME / ".config" / APP_ID
DATA_DIR = HOME / ".local" / "share" / APP_ID
GEOMETRY_FILE = CONFIG_DIR / "geometry"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
ENTRIES_FILE = DATA_DIR / "entries.json"
SYNC_HINT = "Link an account in Settings > Online Accounts to sync a calendar"

DEFAULT_CATEGORIES = [
    {"name": "Work", "color": "#3B82F6"},
    {"name": "Personal", "color": "#22C55E"},
    {"name": "Errand", "color": "#F97316"},
    {"name": "Appointment", "color": "#EF4444"},
    {"name": "Birthday", "color": "#A855F7"},
    {"name": "Other", "color": "#9CA3AF"},
]
OTHER = "Other"
MAX_SPAN_DAYS = 366             # one entry can block up to a year (a long trip)
IMPORTANT_RGB = (0.86, 0.05, 0.08)  # red ! drawn across an important day
DEFAULT_OPACITY = 0.78          # background alpha of the default see-through look
SEE_THROUGH_LEVELS = (20, 40, 60, 80)   # menu / [ ] steps, percent see-through
MIN_OPACITY = 0.15

# GNOME Wayland cannot move a native window. XWayland can, so open through it.
# DESKLET_NATIVE_WAYLAND=1 keeps the Wayland backend.
sys.path[:0] = [
    str(Path(__file__).resolve().parent),
    str(Path.home() / ".local" / "lib"),
]
from desklet_geometry import (
    DeskletChrome,
    DeskletPlacement,
    prefer_xwayland,
    prepare_desktop_window,
)

prefer_xwayland()

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo
    import cairo
except (ImportError, ValueError) as exc:
    sys.stderr.write(
        "calendar-widget: need GTK3 bindings.\n"
        "  sudo apt-get install -y python3-gi python3-gi-cairo gir1.2-gtk-3.0\n"
        f"({exc})\n"
    )
    sys.exit(1)

# ---------------------------------------------------------------- EDS import
EDataServer = ECal = ICalGLib = None  # type: ignore
EDS_IMPORT_ERROR = ""


def _import_eds() -> None:
    global EDataServer, ECal, ICalGLib, EDS_IMPORT_ERROR
    if os.environ.get("CALENDAR_WIDGET_NO_SYNC"):
        EDS_IMPORT_ERROR = "sync disabled (CALENDAR_WIDGET_NO_SYNC)"
        return
    try:
        gi.require_version("EDataServer", "1.2")
    except ValueError:
        EDS_IMPORT_ERROR = "gir1.2-edataserver-1.2 not installed"
        return
    try:
        gi.require_version("ECal", "2.0")
        gi.require_version("ICalGLib", "3.0")
    except ValueError:
        try:
            gi.require_version("ECal", "1.2")
            EDS_IMPORT_ERROR = "only old ECal 1.2 found (need gir1.2-ecal-2.0, EDS >= 3.34)"
        except ValueError:
            EDS_IMPORT_ERROR = "gir1.2-ecal-2.0 / gir1.2-ical-3.0 not installed"
        return
    try:
        from gi.repository import EDataServer as _eds, ECal as _ecal, ICalGLib as _ical

        EDataServer, ECal, ICalGLib = _eds, _ecal, _ical
    except Exception as e:  # noqa: BLE001 - depends on system
        EDS_IMPORT_ERROR = f"EDS bindings failed to load: {e}"


_import_eds()


def log(msg: str) -> None:
    sys.stderr.write(f"calendar-widget: {msg}\n")


# ------------------------------------------------------------------ helpers
def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def hex_to_rgb(color: str) -> Tuple[float, float, float]:
    c = (color or "").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return int(c[0:2], 16) / 255.0, int(c[2:4], 16) / 255.0, int(c[4:6], 16) / 255.0
    except (ValueError, IndexError):
        return 0.6, 0.6, 0.6


def rgba_to_hex(rgba) -> str:
    return "#{:02X}{:02X}{:02X}".format(
        int(round(rgba.red * 255)), int(round(rgba.green * 255)), int(round(rgba.blue * 255))
    )


TIME_RE = re.compile(r"^\s*([01]?\d|2[0-3])\s*[:.h]?\s*([0-5]\d)\s*$")


def parse_hhmm(text: str) -> Optional[str]:
    """'' -> '' (all day); valid time -> 'HH:MM'; invalid -> None."""
    text = (text or "").strip()
    if not text:
        return ""
    m = TIME_RE.match(text)
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}"


def date_key(d: dt.date) -> str:
    return d.isoformat()


def key_date(k: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(k)
    except (TypeError, ValueError):
        return None


def span_days(entry: dict) -> int:
    """How many calendar days an entry covers. Missing or bad values mean one day."""
    try:
        n = int(entry.get("days") or 1)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(n, MAX_SPAN_DAYS))


def days_through_sunday(day: dt.date) -> int:
    """Days from this date through Sunday, counting today. Monday is 7, Sunday is 1."""
    return 7 - day.weekday()


def span_range_text(anchor: dt.date, ndays: int) -> str:
    """Label for a block. One day stays a normal entry; longer blocks are all day."""
    ndays = max(1, min(int(ndays or 1), MAX_SPAN_DAYS))
    if ndays <= 1:
        return "this day only"
    end = anchor + dt.timedelta(days=ndays - 1)
    return f"{anchor.strftime('%-d %b')} to {end.strftime('%-d %b %Y')} · all day"


def entry_sort_key(e: dict):
    return (1 if e.get("time") else 0, e.get("time") or "", (e.get("text") or "").lower())


def run_bg(func: Callable, done: Callable) -> None:
    """Run func() in a thread; call done(result, error) on the GTK main loop."""

    def worker():
        try:
            res, err = func(), None
        except Exception as e:  # noqa: BLE001
            res, err = None, e
        GLib.idle_add(lambda: (done(res, err), False)[1])

    threading.Thread(target=worker, daemon=True).start()


def _opt_coord(token: str) -> Optional[int]:
    if token in {"-", "none", "null", "?"}:
        return None
    return int(token)


def load_geometry() -> Optional[Tuple[Optional[int], Optional[int], int, int]]:
    try:
        parts = GEOMETRY_FILE.read_text(encoding="utf-8").replace(",", " ").split()
        if len(parts) >= 4:
            x, y = _opt_coord(parts[0]), _opt_coord(parts[1])
            w, h = int(parts[2]), int(parts[3])
            if w >= MIN_SIZE and h >= MIN_SIZE:
                return x, y, w, h
    except (OSError, ValueError):
        pass
    return None


def save_geometry(x: int, y: int, w: int, h: int) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        GEOMETRY_FILE.write_text(f"{x} {y} {w} {h}\n", encoding="utf-8")
    except OSError:
        pass


# -------------------------------------------------------------------- store
class Store:
    """Categories, local-only entries and a cache of synced calendar events.

    entries.json layout:
      {"version": 2,
       "categories": [{"name": "Work", "color": "#3B82F6", "aliases": []}, ...],
       "entries": {"2026-09-25": [{"id", "text", "category", "time"}]},   # local
       "important": ["2026-12-25"],   # days that draw a red !
       "cache_source": "<EDS source uid>",
       "cache": {"2026-09-25": [{... "remote": true, "uid", "rid", ...}]}}  # synced
    """

    def __init__(self) -> None:
        self.categories: List[dict] = [dict(c, aliases=[]) for c in DEFAULT_CATEGORIES]
        self.entries: Dict[str, List[dict]] = {}
        self.important: set = set()
        self.cache_source = ""
        self.cache: Dict[str, List[dict]] = {}
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(ENTRIES_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log(f"could not read {ENTRIES_FILE}: {e}")
            try:
                ENTRIES_FILE.rename(ENTRIES_FILE.with_name(f"entries.json.corrupt-{int(time.time())}"))
            except OSError:
                pass
            return
        if not isinstance(data, dict):
            return
        cats = data.get("categories")
        if isinstance(cats, list):
            clean = []
            seen = set()
            for c in cats:
                if isinstance(c, dict) and str(c.get("name", "")).strip():
                    name = str(c["name"]).strip()
                    if name.lower() in seen:
                        continue
                    seen.add(name.lower())
                    aliases = c.get("aliases") if isinstance(c.get("aliases"), list) else []
                    clean.append({"name": name, "color": str(c.get("color") or "#9CA3AF"),
                                  "aliases": [str(a) for a in aliases]})
            if clean:
                self.categories = clean
        self._ensure_other()

        def clean_map(m, remote: bool) -> Dict[str, List[dict]]:
            out: Dict[str, List[dict]] = {}
            if not isinstance(m, dict):
                return out
            for k, lst in m.items():
                if key_date(k) is None or not isinstance(lst, list):
                    continue
                items = []
                for e in lst:
                    if isinstance(e, dict) and str(e.get("text", "")).strip():
                        e = dict(e)
                        e.setdefault("id", uuid.uuid4().hex)
                        e.setdefault("category", OTHER)
                        e.setdefault("time", "")
                        e["remote"] = remote
                        items.append(e)
                if items:
                    out[k] = items
            return out

        self.entries = clean_map(data.get("entries"), False)
        self.cache_source = str(data.get("cache_source") or "")
        self.cache = clean_map(data.get("cache"), True)
        marked = set()
        raw = data.get("important")
        if isinstance(raw, list):
            for item in raw:
                if key_date(str(item)) is not None:
                    marked.add(str(item)[:10])
        self.important = marked

    def save(self) -> None:
        try:
            atomic_write_json(ENTRIES_FILE, {
                "version": 2,
                "categories": self.categories,
                "entries": self.entries,
                "important": sorted(self.important),
                "cache_source": self.cache_source,
                "cache": self.cache,
            })
        except OSError as e:
            log(f"could not save {ENTRIES_FILE}: {e}")

    def _ensure_other(self) -> None:
        if not any(c["name"] == OTHER for c in self.categories):
            self.categories.append({"name": OTHER, "color": "#9CA3AF", "aliases": []})

    def names(self) -> List[str]:
        return [c["name"] for c in self.categories]

    def color(self, name: str) -> Tuple[float, float, float]:
        for c in self.categories:
            if c["name"] == name:
                return hex_to_rgb(c["color"])
        for c in self.categories:
            if c["name"] == OTHER:
                return hex_to_rgb(c["color"])
        return 0.6, 0.6, 0.6

    def match_category(self, names: List[str]) -> str:
        """Map iCal CATEGORIES values to a known category (else Other)."""
        for raw in names:
            n = raw.strip().lower()
            if not n:
                continue
            for c in self.categories:
                if c["name"].lower() == n or n in (a.lower() for a in c.get("aliases", [])):
                    return c["name"]
        return OTHER

    def normalise(self, name: str) -> str:
        return name if name in self.names() else OTHER

    def is_important(self, d: dt.date) -> bool:
        return date_key(d) in self.important

    def set_important(self, d: dt.date, on: bool) -> None:
        key = date_key(d)
        if on == (key in self.important):
            return
        if on:
            self.important.add(key)
        else:
            self.important.discard(key)
        self.save()


# ---------------------------------------------------------------------- EDS
def ical_times(day: dt.date, hhmm: str, ndays: int = 1):
    """All-day DATE block, or a timed 1h event stored in UTC.

    A block of several days is all day. DTEND is exclusive, so a trip that
    starts Monday and covers 7 days ends on the next Monday.
    """
    ndays = max(1, min(int(ndays or 1), MAX_SPAN_DAYS))
    if hhmm and ndays == 1:
        h, m = (int(x) for x in hhmm.split(":"))
        local = dt.datetime(day.year, day.month, day.day, h, m).astimezone()
        s = local.astimezone(dt.timezone.utc)
        e = s + dt.timedelta(hours=1)
        return (ICalGLib.Time.new_from_string(s.strftime("%Y%m%dT%H%M%SZ")),
                ICalGLib.Time.new_from_string(e.strftime("%Y%m%dT%H%M%SZ")))
    end = day + dt.timedelta(days=ndays)
    return (ICalGLib.Time.new_from_string(day.strftime("%Y%m%d")),
            ICalGLib.Time.new_from_string(end.strftime("%Y%m%d")))


def ical_to_local(t) -> Tuple[Optional[dt.date], str]:
    if t is None or t.is_null_time():
        return None, ""
    if t.is_date():
        return dt.date(t.get_year(), t.get_month(), t.get_day()), ""
    if t.is_utc():
        ts = t.as_timet()
    else:
        z = t.get_timezone()
        if z is None:  # floating time = local wall clock
            return (dt.date(t.get_year(), t.get_month(), t.get_day()),
                    f"{t.get_hour():02d}:{t.get_minute():02d}")
        ts = t.as_timet_with_zone(z)
    loc = dt.datetime.fromtimestamp(ts)
    return loc.date(), loc.strftime("%H:%M")


def _props_of_kind(icomp, kind) -> list:
    props = []
    p = icomp.get_first_property(kind)
    while p is not None:
        props.append(p)
        p = icomp.get_next_property(kind)
    return props


def ical_categories(icomp) -> List[str]:
    out: List[str] = []
    for p in _props_of_kind(icomp, ICalGLib.PropertyKind.CATEGORIES_PROPERTY):
        for part in (p.get_categories() or "").split(","):
            if part.strip():
                out.append(part.strip())
    return out


def ical_set_category(icomp, category: str) -> None:
    for p in _props_of_kind(icomp, ICalGLib.PropertyKind.CATEGORIES_PROPERTY):
        icomp.remove_property(p)
    if category:
        icomp.add_property(ICalGLib.Property.new_categories(category))


def ical_remove_kind(icomp, kind) -> None:
    for p in _props_of_kind(icomp, kind):
        icomp.remove_property(p)


class EdsSync:
    """Thin wrapper over ESourceRegistry + ECalClient (EDS / GNOME Online Accounts).

    Blocking calls are meant to run in worker threads (see run_bg).
    """

    SKIP_BACKENDS = ("contacts", "weather", "birthdays", "webcal")

    def __init__(self) -> None:
        self.registry = None
        self._flags = ECal.OperationFlags.NONE

    def ensure_registry(self):
        if self.registry is None:
            self.registry = EDataServer.SourceRegistry.new_sync(None)
        return self.registry

    def calendars(self) -> List[dict]:
        reg = self.ensure_registry()
        out = []
        for src in reg.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR):
            try:
                if not reg.check_enabled(src):
                    continue
                ext = src.get_extension(EDataServer.SOURCE_EXTENSION_CALENDAR)
                backend = (ext.get_backend_name() or "").lower()
                if backend in self.SKIP_BACKENDS:
                    continue
                parent_uid = src.get_parent() or ""
                parent = reg.ref_source(parent_uid) if parent_uid else None
                account = parent.get_display_name() if parent is not None else ""
                google = backend == "google" or "google" in parent_uid.lower()
                if parent is not None and parent.has_extension(EDataServer.SOURCE_EXTENSION_COLLECTION):
                    coll = parent.get_extension(EDataServer.SOURCE_EXTENSION_COLLECTION)
                    if (coll.get_backend_name() or "").lower() == "google":
                        google = True
                out.append({
                    "uid": src.get_uid(),
                    "name": src.get_display_name() or src.get_uid(),
                    "account": "" if parent_uid.endswith("-stub") else account,
                    "backend": backend,
                    "google": google,
                    "remote": backend != "local",
                })
            except Exception as e:  # noqa: BLE001
                log(f"skipping calendar source: {e}")
        out.sort(key=lambda c: (not c["google"], not c["remote"], c["name"].lower()))
        return out

    def connect_source(self, uid: str):
        reg = self.ensure_registry()
        src = reg.ref_source(uid)
        if src is None:
            raise RuntimeError(f"calendar {uid!r} not found")
        client = ECal.Client.connect_sync(src, ECal.ClientSourceType.EVENTS, 15, None)
        if client is None:
            raise RuntimeError("could not open calendar")
        return client

    def backend_refresh(self, client) -> None:
        """Ask EDS to pull fresh data from the server (e.g. Google) if supported."""
        try:
            if client.check_refresh_supported():
                client.refresh_sync(None)
        except Exception as e:  # noqa: BLE001 - offline etc.; cached data still usable
            log(f"server refresh skipped: {e}")

    def fetch(self, client, start: dt.date, end: dt.date) -> List[dict]:
        """Expanded instances (recurrences included) between start and end (exclusive)."""
        st = int(time.mktime(start.timetuple()))
        en = int(time.mktime(end.timetuple()))
        events: List[dict] = []

        def cb(icomp, istart, iend, *_rest):
            try:
                status = icomp.get_first_property(ICalGLib.PropertyKind.STATUS_PROPERTY)
                if status is not None and "CANCEL" in (status.as_ical_string() or "").upper():
                    return True
                sdate, stime = ical_to_local(istart)
                if sdate is None:
                    return True
                edate, _etime = ical_to_local(iend)
                rid_t = icomp.get_recurrenceid()
                rid = rid_t.as_ical_string() if rid_t is not None and not rid_t.is_null_time() else None
                recurring = rid is not None or icomp.get_first_property(
                    ICalGLib.PropertyKind.RRULE_PROPERTY) is not None
                if recurring and not rid:
                    rid = istart.as_ical_string()
                ndays = 1
                if not stime and edate is not None and edate > sdate:
                    ndays = min((edate - sdate).days, MAX_SPAN_DAYS)
                events.append({
                    "uid": icomp.get_uid() or "",
                    "rid": rid,
                    "recurring": bool(recurring),
                    "text": (icomp.get_summary() or "").strip() or "(no title)",
                    "categories": ical_categories(icomp),
                    "start": sdate.isoformat(),
                    "time": stime,
                    "days": ndays,
                })
            except Exception as e:  # noqa: BLE001
                log(f"bad event skipped: {e}")
            return True

        client.generate_instances_sync(st, en, None, cb)
        return events

    def create(self, client, day: dt.date, text: str, category: str, hhmm: str,
               ndays: int = 1) -> str:
        ndays = max(1, min(int(ndays or 1), MAX_SPAN_DAYS))
        if ndays > 1:
            hhmm = ""
        ic = ICalGLib.Component.new(ICalGLib.ComponentKind.VEVENT_COMPONENT)
        ic.set_uid(f"{uuid.uuid4()}@calendar-widget")
        ic.set_summary(text)
        s, e = ical_times(day, hhmm, ndays)
        ic.set_dtstart(s)
        ic.set_dtend(e)
        ic.set_dtstamp(ICalGLib.Time.new_current_with_zone(ICalGLib.Timezone.get_utc_timezone()))
        ical_set_category(ic, category)
        ok, uid = client.create_object_sync(ic, self._flags, None)
        if not ok:
            raise RuntimeError("calendar refused the new event")
        return uid or ic.get_uid()

    def modify(self, client, entry: dict, text: str, category: str, hhmm: str,
               ndays: int = 1) -> None:
        ndays = max(1, min(int(ndays or 1), MAX_SPAN_DAYS))
        if ndays > 1:
            hhmm = ""
        ok, ic = client.get_object_sync(entry["uid"], None, None)
        if not ok or ic is None:
            raise RuntimeError("event not found (deleted elsewhere?)")
        ic.set_summary(text)
        ical_set_category(ic, category)
        stored = entry.get("days")
        length_changed = (stored is not None and ndays != span_days(entry)) or (stored is None and ndays > 1)
        if hhmm != (entry.get("time") or "") or length_changed:
            day = key_date(entry.get("start") or "") or dt.date.today()
            use_days = ndays if length_changed else 1
            s, e = ical_times(day, hhmm, use_days)
            ical_remove_kind(ic, ICalGLib.PropertyKind.DURATION_PROPERTY)
            ic.set_dtstart(s)
            ic.set_dtend(e)
        if not client.modify_object_sync(ic, ECal.ObjModType.ALL, self._flags, None):
            raise RuntimeError("calendar refused the change")

    def remove(self, client, entry: dict) -> None:
        if entry.get("recurring") and entry.get("rid"):
            ok = client.remove_object_sync(entry["uid"], entry["rid"], ECal.ObjModType.THIS,
                                           self._flags, None)
        else:
            ok = client.remove_object_sync(entry["uid"], None, ECal.ObjModType.ALL, self._flags, None)
        if not ok:
            raise RuntimeError("calendar refused the delete")


# ------------------------------------------------------------------ drawing
def rounded_rect(cr, x, y, w, h, r) -> None:
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def show_text(cr, text, x, y, size_px, rgba, bold=False, width=None, align=None, vcenter=False,
              shadow=0.0):
    layout = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string("Sans Bold" if bold else "Sans")
    fd.set_absolute_size(max(1.0, size_px) * Pango.SCALE)
    layout.set_font_description(fd)
    layout.set_single_paragraph_mode(True)
    if width is not None:
        layout.set_width(int(max(1.0, width) * Pango.SCALE))
        layout.set_ellipsize(Pango.EllipsizeMode.END)
    if align is not None:
        layout.set_alignment(align)
    layout.set_text(text, -1)
    _ink, logical = layout.get_pixel_extents()
    if vcenter:
        y = y - logical.height / 2.0
    if shadow > 0:  # soft dark offset copy keeps text readable over bright wallpaper
        off = max(1.0, size_px / 14.0)
        cr.set_source_rgba(0, 0, 0, shadow)
        cr.move_to(x + off, y + off)
        PangoCairo.show_layout(cr, layout)
    cr.set_source_rgba(*rgba)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return logical


def draw_important_mark(cr, x: float, y: float, s: float, alpha: float = 1.0) -> None:
    """Bold red ! centred across a day cell, with a light outline."""
    size = max(12.0, s * 0.78)
    layout = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string("Sans Bold")
    fd.set_absolute_size(size * Pango.SCALE)
    layout.set_font_description(fd)
    layout.set_text("!", -1)
    ink, _logical = layout.get_pixel_extents()
    cr.save()
    cr.move_to(x + (s - ink.width) / 2.0 - ink.x, y + (s - ink.height) / 2.0 - ink.y)
    PangoCairo.layout_path(cr, layout)
    cr.set_line_width(max(1.4, s * 0.04))
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.set_source_rgba(1, 1, 1, 0.95 * alpha)
    cr.stroke_preserve()
    cr.set_source_rgba(*IMPORTANT_RGB, 0.96 * alpha)
    cr.fill()
    cr.restore()


# ------------------------------------------------------------- main window
class CalendarWidget(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="Calendar Widget")
        self.store = Store()
        self.settings = self._load_settings()
        today = dt.date.today()
        self._today = today
        self.year, self.month = today.year, today.month
        self.hover: Optional[str] = None  # "prev" | "next" | "title" | "YYYY-MM-DD"
        self.day_dialog: Optional["DayDialog"] = None
        self._geo_save_id: Optional[int] = None
        self._osd_text = ""
        self._osd_id: Optional[int] = None

        # sync state
        self.eds: Optional[EdsSync] = EdsSync() if ECal is not None else None
        self.calendars: List[dict] = []
        self.client = None
        self.client_uid = ""
        self.client_readonly = False
        self.sync_state = "unavailable" if self.eds is None else "local"
        self.sync_msg = EDS_IMPORT_ERROR if self.eds is None else ""
        self.last_sync: Optional[float] = None
        self._fetch_gen = 0
        self._connecting = False
        self._refresh_pending: Optional[int] = None

        self.set_resizable(True)
        self.set_app_paintable(True)
        self.set_icon_name("x-office-calendar")
        self.composited = False
        self._update_visual()
        prepare_desktop_window(self)
        if self.settings.get("keep_above"):
            self.settings["keep_above"] = False
            self.save_settings()

        geom = Gdk.Geometry()
        geom.min_width = geom.min_height = MIN_SIZE
        geom.min_aspect = geom.max_aspect = 1.0
        self.set_geometry_hints(None, geom, Gdk.WindowHints.MIN_SIZE | Gdk.WindowHints.ASPECT)

        self._place = DeskletPlacement(
            self,
            load_geometry,
            save_geometry,
            min_w=MIN_SIZE,
            min_h=MIN_SIZE,
            on_quit=self.quit,
        )
        if self._place.saved:
            x, y, w, h = self._place.saved
            side = max(MIN_SIZE, min(int(w), int(h)))
            self._place.saved = (x, y, side, side)
            self._place.apply_before_show()
        else:
            self.set_default_size(DEFAULT_SIZE, DEFAULT_SIZE)
            self.set_position(Gtk.WindowPosition.CENTER)

        self.area = Gtk.DrawingArea()
        self.area.set_can_focus(True)
        self.area.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.SCROLL_MASK
        )
        self.area.connect("draw", self._on_draw)
        self.area.connect("button-press-event", self._on_button_press)
        self.area.connect("motion-notify-event", self._on_motion)
        self.area.connect("leave-notify-event", self._on_leave)
        self.area.connect("scroll-event", self._on_scroll)
        self.add(self.area)
        self._chrome = DeskletChrome(
            self,
            min_w=MIN_SIZE,
            min_h=MIN_SIZE,
            grip=EDGE_GRIP,
            click_slop=8,
            square=True,
            on_click=self._on_desklet_click,
            hit_test=lambda _widget, event: self._hit(event.x, event.y),
            placement=self._place,
            manage_cursor=False,
        )
        self._chrome.watch(self.area)
        self.connect("key-press-event", self._on_key_press)
        self.connect("configure-event", self._on_configure)
        self.connect("delete-event", self._on_delete)
        self.connect("screen-changed", lambda *_: self._update_visual())

        GLib.timeout_add_seconds(REFRESH_SECONDS, self._on_tick)
        GLib.timeout_add_seconds(30, self._midnight_check)
        if self.eds is not None:
            GLib.idle_add(self._start_sync)

    def _update_visual(self) -> None:
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        self.composited = bool(visual is not None and screen.is_composited())
        if self.composited:
            self.set_visual(visual)
        if getattr(self, "area", None) is not None:
            self.area.queue_draw()

    # ------------------------------------------------------------ settings
    def _load_settings(self) -> dict:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def save_settings(self) -> None:
        try:
            atomic_write_json(SETTINGS_FILE, self.settings)
        except OSError as e:
            log(f"could not save settings: {e}")

    @property
    def transparent(self) -> bool:
        return bool(self.settings.get("transparent", True))

    @property
    def opacity(self) -> float:
        """Stored background alpha while transparency is on (settings key 'opacity')."""
        try:
            v = float(self.settings.get("opacity", DEFAULT_OPACITY))
        except (TypeError, ValueError):
            v = DEFAULT_OPACITY
        return min(1.0, max(MIN_OPACITY, v))

    def see_through_pct(self) -> int:
        return int(round((1.0 - self.opacity) * 100))

    def _look(self) -> dict:
        """Alpha factors for the current look. Solid when off or not composited."""
        if not (self.composited and self.transparent):
            return {"bg": 1.0, "fill": 1.0, "cat": 1.0, "on": False, "shadow": 0.0}
        op = self.opacity
        f = min(1.0, op / DEFAULT_OPACITY)  # 1.0 at the default look, smaller when more see-through
        return {"bg": op, "fill": f, "cat": max(0.3, f), "on": True,
                "shadow": min(0.6, (1.0 - op) * 0.75)}

    def _ta(self, a: float, look: dict, primary: bool = True) -> float:
        """Text alpha: unchanged when solid; kept readable (>= 0.85) when see-through."""
        if not look["on"]:
            return a
        return max(a, 0.85) if primary else max(a, 0.45)

    def set_transparency(self, on: Optional[bool] = None, see_through: Optional[int] = None) -> None:
        if see_through is not None:
            self.settings["opacity"] = round(1.0 - see_through / 100.0, 2)
            on = True if on is None else on
        if on is not None:
            self.settings["transparent"] = bool(on)
        self.save_settings()
        self.area.queue_draw()
        if not self.composited:
            self._osd("Transparency needs a compositor")
        elif self.transparent:
            self._osd(f"{self.see_through_pct()}% see-through")
        else:
            self._osd("Solid")

    def step_transparency(self, direction: int) -> None:
        cur = self.see_through_pct() if self.transparent else 0
        if direction > 0:
            nxt = next((lv for lv in SEE_THROUGH_LEVELS if lv > cur + 1), SEE_THROUGH_LEVELS[-1])
        else:
            nxt = next((lv for lv in reversed(SEE_THROUGH_LEVELS) if lv < cur - 1), SEE_THROUGH_LEVELS[0])
        self.set_transparency(True, see_through=nxt)

    def _osd(self, text: str) -> None:
        self._osd_text = text
        if self._osd_id is not None:
            GLib.source_remove(self._osd_id)
        self._osd_id = GLib.timeout_add(1300, self._osd_clear)
        self.area.queue_draw()

    def _osd_clear(self) -> bool:
        self._osd_id = None
        self._osd_text = ""
        self.area.queue_draw()
        return False

    # -------------------------------------------------------------- layout
    def _layout(self) -> dict:
        w = self.area.get_allocated_width()
        h = self.area.get_allocated_height()
        pad = max(6.0, min(w, h) * 0.018)
        side = max(1.0, min(w, h) - 2 * pad)
        c = side / 7.0
        header_h = c * 0.62
        ox = (w - side) / 2.0
        oy = (h - side) / 2.0
        return {"w": w, "h": h, "S": side, "ox": ox, "oy": oy, "c": c,
                "header_h": header_h, "wk_h": c - header_h, "grid_y": oy + c}

    def grid_dates(self) -> List[dt.date]:
        first = dt.date(self.year, self.month, 1)
        start = first - dt.timedelta(days=first.weekday())
        return [start + dt.timedelta(days=i) for i in range(42)]

    def _hit(self, x: float, y: float) -> Optional[str]:
        L = self._layout()
        ox, oy, c, S, hh = L["ox"], L["oy"], L["c"], L["S"], L["header_h"]
        if oy <= y <= oy + hh:
            if ox <= x <= ox + c:
                return "prev"
            if ox + S - c <= x <= ox + S:
                return "next"
            if ox + c * 1.6 <= x <= ox + S - c * 1.6:
                return "title"
            return None
        gy = L["grid_y"]
        if ox <= x < ox + S and gy <= y < gy + 6 * c:
            col = int((x - ox) // c)
            row = int((y - gy) // c)
            if 0 <= col < 7 and 0 <= row < 6:
                return date_key(self.grid_dates()[row * 7 + col])
        return None

    # --------------------------------------------------------------- data
    def selected_uid(self) -> str:
        v = self.settings.get("calendar_source")
        if v is None:  # never chosen: automatic = first Google calendar, else local only
            for c in self.calendars:
                if c["google"]:
                    return c["uid"]
            return ""
        return str(v)

    def sync_active(self) -> bool:
        uid = self.selected_uid()
        return bool(uid) and self.store.cache_source == uid

    def _local_on(self, d: dt.date) -> List[dict]:
        """Local entries whose block covers d. A multi-day entry is stored once, on its first day."""
        items: List[dict] = []
        for k, lst in self.store.entries.items():
            start = key_date(k)
            if start is None:
                continue
            for e in lst:
                n = span_days(e)
                if start <= d < start + dt.timedelta(days=n):
                    view = dict(e)
                    view["days"] = n
                    view["start"] = date_key(start)
                    view["span"] = n > 1
                    items.append(view)
        return items

    def entries_for(self, d: dt.date) -> List[dict]:
        items = self._local_on(d)
        if self.sync_active():
            items += self.store.cache.get(date_key(d), [])
        return sorted(items, key=entry_sort_key)

    def can_write_remote(self) -> bool:
        return (self.client is not None and not self.client_readonly
                and self.client_uid == self.selected_uid() and self.sync_active())

    def calendar_name(self) -> str:
        uid = self.selected_uid()
        for c in self.calendars:
            if c["uid"] == uid:
                return c["name"]
        return "online calendar"

    def add_entry(self, d: dt.date, text: str, category: str, hhmm: str,
                  done: Callable[[Optional[str]], None], ndays: int = 1) -> None:
        category = self.store.normalise(category)
        ndays = max(1, min(int(ndays or 1), MAX_SPAN_DAYS))
        if ndays > 1:
            hhmm = ""

        def add_local(note: Optional[str]) -> None:
            rec = {"id": uuid.uuid4().hex, "text": text, "category": category,
                   "time": hhmm, "remote": False}
            if ndays > 1:
                rec["days"] = ndays
            self.store.entries.setdefault(date_key(d), []).append(rec)
            self.store.save()
            self.area.queue_draw()
            done(note)

        if not self.can_write_remote():
            note = None
            if self.selected_uid() and self.eds is not None:
                note = f"{self.calendar_name()} not connected - saved on this computer only."
            add_local(note)
            return
        client, eds = self.client, self.eds

        def finished(uid, err):
            if err is not None:
                log(f"create failed: {err}")
                add_local(f"Couldn't save to {self.calendar_name()} ({err}); saved locally only.")
                return
            for i in range(ndays):
                day = d + dt.timedelta(days=i)
                self.store.cache.setdefault(date_key(day), []).append(
                    {"id": uuid.uuid4().hex, "uid": uid, "rid": None, "recurring": False,
                     "text": text, "category": category, "time": hhmm, "remote": True,
                     "start": date_key(d), "days": ndays, "span": ndays > 1})
            self.store.save()
            self.area.queue_draw()
            done(None)
            self.refresh(delay_ms=500)

        run_bg(lambda: eds.create(client, d, text, category, hhmm, ndays), finished)

    def update_entry(self, d: dt.date, entry: dict, text: str, category: str, hhmm: str,
                     done: Callable[[Optional[str]], None], ndays: int = 1) -> None:
        category = self.store.normalise(category)
        ndays = max(1, min(int(ndays or 1), MAX_SPAN_DAYS))
        if ndays > 1:
            hhmm = ""
        if not entry.get("remote"):
            found = False
            for lst in self.store.entries.values():
                for e in lst:
                    if e.get("id") == entry.get("id"):
                        e.update(text=text, category=category, time=hhmm)
                        if ndays > 1:
                            e["days"] = ndays
                        else:
                            e.pop("days", None)
                        found = True
            if not found:
                log(f"local entry {entry.get('id')} missing while editing")
            self.store.save()
            self.area.queue_draw()
            done(None)
            return
        if not self.can_write_remote():
            done("Couldn't update: calendar is offline or read-only - try again later.")
            return
        client, eds = self.client, self.eds

        def finished(_res, err):
            if err is not None:
                done(f"Couldn't update: {err}")
                return
            for lst in self.store.cache.values():
                for e in lst:
                    if e.get("uid") == entry.get("uid"):
                        e.update(text=text, category=category, time=hhmm, days=ndays, span=ndays > 1)
            self.store.save()
            self.area.queue_draw()
            done(None)
            self.refresh(delay_ms=500)

        run_bg(lambda: eds.modify(client, entry, text, category, hhmm, ndays), finished)

    def delete_entry(self, d: dt.date, entry: dict, done: Callable[[Optional[str]], None]) -> None:
        if not entry.get("remote"):
            for k in list(self.store.entries):
                kept = [e for e in self.store.entries[k] if e.get("id") != entry.get("id")]
                if kept:
                    self.store.entries[k] = kept
                else:
                    del self.store.entries[k]
            self.store.save()
            self.area.queue_draw()
            done(None)
            return
        if not self.can_write_remote():
            done("Couldn't delete: calendar is offline or read-only - try again later.")
            return
        client, eds = self.client, self.eds

        def finished(_res, err):
            if err is not None:
                done(f"Couldn't delete: {err}")
                return
            for k in list(self.store.cache):
                self.store.cache[k] = [
                    e for e in self.store.cache[k]
                    if not (e.get("uid") == entry.get("uid")
                            and (not entry.get("recurring") or e.get("rid") == entry.get("rid")))]
                if not self.store.cache[k]:
                    del self.store.cache[k]
            self.store.save()
            self.area.queue_draw()
            done(None)
            self.refresh(delay_ms=500)

        run_bg(lambda: eds.remove(client, entry), finished)

    # ---------------------------------------------------------------- sync
    def _start_sync(self) -> bool:
        if self.eds is None:
            return False
        eds = self.eds

        def finished(cals, err):
            if err is not None:
                self.sync_state = "unavailable"
                self.sync_msg = f"Evolution Data Server not reachable ({err})"
                log(self.sync_msg)
                self.area.queue_draw()
                return
            self.calendars = cals or []
            self._ensure_client()

        run_bg(eds.calendars, finished)
        return False

    def _ensure_client(self) -> None:
        uid = self.selected_uid()
        if not uid:
            self.client, self.client_uid = None, ""
            self.sync_state, self.sync_msg = "local", ""
            self.area.queue_draw()
            return
        if self.client is not None and self.client_uid == uid:
            self.refresh()
            return
        if self._connecting:
            return
        self._connecting = True
        self.sync_state = "connecting"
        self.area.queue_draw()
        eds = self.eds

        def work():
            client = eds.connect_source(uid)
            return client, bool(client.is_readonly())

        def finished(res, err):
            self._connecting = False
            if uid != self.selected_uid():  # choice changed meanwhile
                self._ensure_client()
                return
            if err is not None:
                self.client, self.client_uid = None, ""
                self.sync_state = "error"
                self.sync_msg = f"Could not open {self.calendar_name()}: {err}"
                log(self.sync_msg)
                self.area.queue_draw()
                return
            self.client, self.client_readonly = res
            self.client_uid = uid
            if self.store.cache_source != uid:
                self.store.cache_source = uid
                self.store.cache = {}
                self.store.save()
            self.refresh()

        run_bg(work, finished)

    def refresh(self, delay_ms: int = 0) -> None:
        """Re-read the visible grid (6 weeks) from EDS."""
        if self._refresh_pending is not None:
            GLib.source_remove(self._refresh_pending)
            self._refresh_pending = None
        if delay_ms:
            def later():
                self._refresh_pending = None
                self.refresh()
                return False
            self._refresh_pending = GLib.timeout_add(delay_ms, later)
            return
        if self.client is None or self.eds is None:
            return
        dates = self.grid_dates()
        start, end = dates[0], dates[-1] + dt.timedelta(days=1)
        self._fetch_gen += 1
        gen = self._fetch_gen
        client, uid, eds = self.client, self.client_uid, self.eds
        fetch_start = start - dt.timedelta(days=31)  # multi-day events spanning into the grid

        def finished(events, err):
            if gen != self._fetch_gen or uid != self.client_uid:
                return
            if err is not None:
                self.sync_state = "error"
                self.sync_msg = f"Sync failed: {err}"
                log(self.sync_msg)
                self.area.queue_draw()
                return
            self._apply_events(events or [], start, end)
            self.sync_state, self.sync_msg = "ok", ""
            self.last_sync = time.time()
            self.area.queue_draw()
            if self.day_dialog is not None and not self.day_dialog.busy:
                self.day_dialog.reload()

        run_bg(lambda: eds.fetch(client, fetch_start, end), finished)

    def _apply_events(self, events: List[dict], start: dt.date, end: dt.date) -> None:
        new: Dict[str, List[dict]] = {}
        for ev in events:
            sdate = key_date(ev["start"])
            if sdate is None:
                continue
            cat = self.store.match_category(ev.get("categories") or [])
            ndays = max(1, ev.get("days", 1))
            for i in range(ndays):
                d = sdate + dt.timedelta(days=i)
                if not (start <= d < end):
                    continue
                new.setdefault(date_key(d), []).append({
                    "id": f"{ev['uid']}|{ev.get('rid') or ''}|{i}",
                    "uid": ev["uid"], "rid": ev.get("rid"), "recurring": ev.get("recurring", False),
                    "text": ev["text"], "category": cat, "time": ev.get("time", ""),
                    "remote": True, "start": ev["start"], "days": ndays, "span": ndays > 1,
                    "raw_categories": ev.get("categories") or [],
                })
        changed = False
        d = start
        while d < end:
            k = date_key(d)
            old, cur = self.store.cache.get(k), new.get(k)
            if (old or None) != (cur or None):
                changed = True
                if cur:
                    self.store.cache[k] = cur
                else:
                    self.store.cache.pop(k, None)
            d += dt.timedelta(days=1)
        if changed:
            self.store.save()

    def select_calendar(self, uid: str) -> None:
        self.settings["calendar_source"] = uid
        self.save_settings()
        self.client, self.client_uid = None, ""
        self._ensure_client()

    def move_local_to_remote(self) -> None:
        if not self.can_write_remote():
            return
        items = [(k, dict(e)) for k, lst in self.store.entries.items() for e in lst]
        if not items:
            return
        client, eds, store = self.client, self.eds, self.store

        def work():
            moved, errors = [], []
            for k, e in items:
                try:
                    eds.create(client, key_date(k), e["text"], store.normalise(e.get("category", OTHER)),
                               e.get("time", ""), span_days(e))
                    moved.append((k, e.get("id")))
                except Exception as ex:  # noqa: BLE001
                    errors.append(str(ex))
            return moved, errors

        def finished(res, err):
            if err is not None:
                self._message(f"Move failed: {err}")
                return
            moved, errors = res
            for k, eid in moved:
                self.store.entries[k] = [x for x in self.store.entries.get(k, []) if x.get("id") != eid]
                if not self.store.entries[k]:
                    del self.store.entries[k]
            self.store.save()
            self.refresh()
            msg = f"Moved {len(moved)} entr{'y' if len(moved) == 1 else 'ies'} to {self.calendar_name()}."
            if errors:
                msg += f"\n{len(errors)} failed: {errors[0]}"
            self._message(msg)

        run_bg(work, finished)

    def _on_tick(self) -> bool:
        if self.eds is not None and self.sync_state != "unavailable":
            if self.client is None and not self._connecting:
                self._start_sync()  # picks up newly linked accounts / retries errors
            else:
                client, eds = self.client, self.eds
                run_bg(lambda: eds.backend_refresh(client), lambda *_: self.refresh(delay_ms=1500))
        return True

    def _midnight_check(self) -> bool:
        if dt.date.today() != self._today:
            self._today = dt.date.today()
            self.area.queue_draw()
        return True

    # ----------------------------------------------------------- navigation
    def shift_month(self, delta: int) -> None:
        m = self.month - 1 + delta
        self.year += m // 12
        self.month = m % 12 + 1
        self.area.queue_draw()
        self.refresh(delay_ms=250)

    def go_today(self, *_a) -> None:
        t = dt.date.today()
        if (t.year, t.month) != (self.year, self.month):
            self.year, self.month = t.year, t.month
            self.refresh(delay_ms=100)
        self.area.queue_draw()

    # --------------------------------------------------------------- events
    def _edge_at(self, x: float, y: float):
        w = self.area.get_allocated_width()
        h = self.area.get_allocated_height()
        g = EDGE_GRIP
        left, right, top, bottom = x <= g, x >= w - g, y <= g, y >= h - g
        if top and left:
            return Gdk.WindowEdge.NORTH_WEST
        if top and right:
            return Gdk.WindowEdge.NORTH_EAST
        if bottom and left:
            return Gdk.WindowEdge.SOUTH_WEST
        if bottom and right:
            return Gdk.WindowEdge.SOUTH_EAST
        if left:
            return Gdk.WindowEdge.WEST
        if right:
            return Gdk.WindowEdge.EAST
        if top:
            return Gdk.WindowEdge.NORTH
        if bottom:
            return Gdk.WindowEdge.SOUTH
        return None

    _CURSORS = {
        "NORTH_WEST": "nwse-resize", "SOUTH_EAST": "nwse-resize",
        "NORTH_EAST": "nesw-resize", "SOUTH_WEST": "nesw-resize",
        "EAST": "ew-resize", "WEST": "ew-resize", "NORTH": "ns-resize", "SOUTH": "ns-resize",
    }

    def _on_motion(self, _w, event) -> bool:
        edge = self._edge_at(event.x, event.y)
        hit = None if edge is not None else self._hit(event.x, event.y)
        if edge is not None:
            name = self._CURSORS.get(edge.value_nick.upper().replace("-", "_"), "default")
        else:
            name = "pointer" if hit else "default"
        gw = self.area.get_window()
        if gw is not None:
            gw.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), name))
        if hit != self.hover:
            self.hover = hit
            self.area.queue_draw()
        return False

    def _on_leave(self, *_a) -> bool:
        if self.hover is not None:
            self.hover = None
            self.area.queue_draw()
        return False

    def _on_scroll(self, _w, event) -> bool:
        if event.direction == Gdk.ScrollDirection.UP:
            self.shift_month(-1)
        elif event.direction == Gdk.ScrollDirection.DOWN:
            self.shift_month(1)
        return True

    def _on_button_press(self, _w, event) -> bool:
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return True  # swallow double/triple-click duplicates
        if event.button == 3:
            self._popup_menu(event)
            return True
        return False

    def _on_desklet_click(self, _widget, _event, hit) -> None:
        if hit == "prev":
            self.shift_month(-1)
        elif hit == "next":
            self.shift_month(1)
        elif hit == "title":
            self.go_today()
        elif hit:
            d = key_date(hit)
            if d is not None:
                self.open_day(d)

    def _on_key_press(self, _w, event) -> bool:
        key = (Gdk.keyval_name(event.keyval) or "").lower()
        if key in ("escape", "q"):
            self.quit()
        elif key in ("left", "page_up", "kp_left", "kp_page_up"):
            self.shift_month(-1)
        elif key in ("right", "page_down", "kp_right", "kp_page_down"):
            self.shift_month(1)
        elif key == "up":
            self.shift_month(-12)
        elif key == "down":
            self.shift_month(12)
        elif key in ("t", "home"):
            self.go_today()
        elif key == "o":
            self.set_transparency(not self.transparent)
        elif key == "bracketright":
            self.step_transparency(+1)
        elif key == "bracketleft":
            self.step_transparency(-1)
        elif key in ("f5", "r"):
            self._on_tick()
        else:
            return False
        return True

    def _on_configure(self, *_a) -> bool:
        return False

    def _on_delete(self, *_a) -> bool:
        self.quit()
        return True

    def quit(self, *_a) -> None:
        self._place._persist()
        Gtk.main_quit()

    # ----------------------------------------------------------------- menu
    def _popup_menu(self, event) -> None:
        menu = Gtk.Menu()
        menu.attach_to_widget(self, None)

        hit = self._hit(event.x, event.y)
        day = key_date(hit) if isinstance(hit, str) else None
        if day is not None:
            chk = Gtk.CheckMenuItem(label="Important")
            chk.set_active(self.store.is_important(day))
            chk.connect("toggled", lambda w, day=day: self._set_day_important(day, w.get_active()))
            menu.append(chk)
            menu.append(Gtk.SeparatorMenuItem())

        def item(label, cb=None):
            mi = Gtk.MenuItem(label=label)
            mi.set_sensitive(cb is not None)
            if cb is not None:
                mi.connect("activate", lambda *_: cb())
            menu.append(mi)
            return mi

        item("Today", self.go_today)
        item("Manage categories…", lambda: self.open_categories(self))
        menu.append(Gtk.SeparatorMenuItem())

        acct = Gtk.MenuItem(label="Calendar account")
        sub = Gtk.Menu()
        acct.set_submenu(sub)
        menu.append(acct)
        group = None
        current = self.selected_uid()
        choices = [("", "Local only (no sync)")]
        for c in self.calendars:
            label = c["name"]
            if c["account"] and c["account"] != c["name"]:
                label += f"  —  {c['account']}"
            if c["google"]:
                label += "  (Google)"
            elif not c["remote"]:
                label += "  (this computer only)"
            choices.append((c["uid"], label))
        for uid, label in choices:
            ri = Gtk.RadioMenuItem.new_with_label_from_widget(group, label)
            group = ri
            ri.set_active(uid == current)
            ri.connect("toggled", self._on_calendar_toggled, uid)
            sub.append(ri)
        sub.append(Gtk.SeparatorMenuItem())
        rescan = Gtk.MenuItem(label="Rescan accounts")
        rescan.connect("activate", lambda *_: self._start_sync())
        sub.append(rescan)
        acct.set_sensitive(self.eds is not None)

        item("Sync now", self._on_tick if self.client is not None else None)
        n_local = sum(len(v) for v in self.store.entries.values())
        if n_local and self.can_write_remote():
            item(f"Move {n_local} local entr{'y' if n_local == 1 else 'ies'} to {self.calendar_name()}…",
                 self._confirm_move_local)
        item(self._status_text())
        if self.eds is None or not any(c["remote"] for c in self.calendars):
            item(SYNC_HINT)
        menu.append(Gtk.SeparatorMenuItem())

        tr = Gtk.CheckMenuItem(label="Transparent background" if self.composited
                               else "Transparent background (needs compositing)")
        tr.set_active(self.transparent)
        tr.connect("toggled", lambda w: self.set_transparency(w.get_active()))
        menu.append(tr)
        lvl = Gtk.MenuItem(label="Transparency level")
        lsub = Gtk.Menu()
        lvl.set_submenu(lsub)
        cur = self.see_through_pct()
        nearest = min(SEE_THROUGH_LEVELS, key=lambda lv: abs(lv - cur))
        group = None
        for lv in SEE_THROUGH_LEVELS:
            ri = Gtk.RadioMenuItem.new_with_label_from_widget(group, f"{lv}% see-through")
            group = ri
            ri.set_active(lv == nearest)
            ri.connect("toggled", lambda w, v=lv: w.get_active() and self.set_transparency(True, see_through=v))
            lsub.append(ri)
        lsub.append(Gtk.SeparatorMenuItem())
        hint = Gtk.MenuItem(label="Keys: o on/off,  [ less,  ] more")
        hint.set_sensitive(False)
        lsub.append(hint)
        menu.append(lvl)
        menu.append(Gtk.SeparatorMenuItem())
        item("Quit", self.quit)
        menu.show_all()
        menu.popup_at_pointer(event)

    def _set_day_important(self, day: dt.date, on: bool) -> None:
        self.store.set_important(day, on)
        self.area.queue_draw()
        dlg = self.day_dialog
        if dlg is not None and dlg.day == day and dlg.important.get_active() != on:
            dlg.important.handler_block_by_func(dlg._on_important)
            dlg.important.set_active(on)
            dlg.important.handler_unblock_by_func(dlg._on_important)

    def _on_calendar_toggled(self, widget, uid: str) -> None:
        if widget.get_active() and (uid != self.selected_uid() or self.settings.get("calendar_source") is None):
            self.select_calendar(uid)

    def _status_text(self) -> str:
        s = self.sync_state
        if s == "unavailable":
            return f"Local only — {self.sync_msg or 'sync not available'}"[:100]
        if s == "local":
            return "Local only (entries saved on this computer)"
        if s == "connecting":
            return f"Connecting to {self.calendar_name()}…"
        if s == "error":
            return (self.sync_msg or "Sync error")[:100]
        when = time.strftime("%H:%M", time.localtime(self.last_sync)) if self.last_sync else "?"
        ro = " (read-only)" if self.client_readonly else ""
        return f"Synced with {self.calendar_name()}{ro} at {when}"

    def _set_setting(self, key, value) -> None:
        self.settings[key] = value
        self.save_settings()
        self.area.queue_draw()

    def _confirm_move_local(self) -> None:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                text=f"Move local entries to {self.calendar_name()}?")
        dlg.format_secondary_text("They will be created in the online calendar "
                                  "and removed from the local-only list.")
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.OK:
            self.move_local_to_remote()

    def _message(self, text: str) -> None:
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                                buttons=Gtk.ButtonsType.OK, text=text)
        dlg.run()
        dlg.destroy()

    # -------------------------------------------------------------- dialogs
    def open_day(self, d: dt.date) -> None:
        if self.day_dialog is not None:
            self.day_dialog.destroy()
        dlg = DayDialog(self, d)
        self.day_dialog = dlg
        dlg.connect("destroy", self._on_day_dialog_closed)
        dlg.show_all()
        dlg.after_show()

    def _on_day_dialog_closed(self, dlg) -> None:
        if self.day_dialog is dlg:
            self.day_dialog = None

    def open_categories(self, parent) -> None:
        dlg = CategoryDialog(parent, self.store)
        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK or dlg.validate():
                break
        if resp == Gtk.ResponseType.OK:
            dlg.apply()
            self.store.save()
            self.area.queue_draw()
            if self.day_dialog is not None:
                self.day_dialog.reload(categories=True)
        dlg.destroy()

    # ------------------------------------------------------------------ draw
    def _on_draw(self, _w, cr) -> bool:
        look = self._look()
        self._cur_look = look
        L = self._layout()
        w, h = L["w"], L["h"]
        ox, oy, c, S = L["ox"], L["oy"], L["c"], L["S"]
        hh, wk = L["header_h"], L["wk_h"]

        cr.set_operator(cairo.OPERATOR_SOURCE)
        if self.composited:
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()
            rounded_rect(cr, 0, 0, w, h, min(18.0, min(w, h) * 0.04))
            cr.set_source_rgba(0.07, 0.08, 0.10, look["bg"])
            cr.fill()
        else:
            cr.set_source_rgb(0.07, 0.08, 0.10)
            cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        # header: month title + white semi-transparent chevrons
        title = f"{calendar.month_name[self.month]} {self.year}"
        show_text(cr, title, ox, oy + hh / 2, hh * 0.46,
                  (1, 1, 1, 1.0 if self.hover == "title" else 0.93), bold=True,
                  width=S, align=Pango.Alignment.CENTER, vcenter=True, shadow=look["shadow"])
        self._draw_chevron(cr, ox + c / 2, oy + hh / 2, hh * 0.24, True, self.hover == "prev")
        self._draw_chevron(cr, ox + S - c / 2, oy + hh / 2, hh * 0.24, False, self.hover == "next")

        dot = {"ok": (0.30, 0.85, 0.45), "connecting": (0.95, 0.75, 0.25),
               "error": (0.95, 0.40, 0.30)}.get(self.sync_state)
        if dot is not None:  # tiny sync status light, top-right
            cr.arc(ox + S - 4, oy + 4, max(2.5, c * 0.035), 0, 2 * math.pi)
            cr.set_source_rgba(dot[0], dot[1], dot[2], 0.9)
            cr.fill()

        for i, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            show_text(cr, name, ox + i * c, oy + hh + wk / 2, wk * 0.46,
                      (1, 1, 1, self._ta(0.62 if i < 5 else 0.48, look)), bold=True, width=c,
                      align=Pango.Alignment.CENTER, vcenter=True, shadow=look["shadow"])

        today = dt.date.today()
        gy = L["grid_y"]
        for idx, d in enumerate(self.grid_dates()):
            row, col = divmod(idx, 7)
            self._draw_cell(cr, d, ox + col * c, gy + row * c, c, d == today)

        if self._osd_text:  # brief on-screen note (transparency changes)
            fs_osd = max(11.0, c * 0.17)
            layout_w = S * 0.6
            bx, by, bh = ox + (S - layout_w) / 2, oy + S - fs_osd * 3.2, fs_osd * 2.0
            rounded_rect(cr, bx, by, layout_w, bh, bh / 2)
            cr.set_source_rgba(0, 0, 0, 0.72)
            cr.fill()
            show_text(cr, self._osd_text, bx, by + bh / 2, fs_osd, (1, 1, 1, 0.97), bold=True,
                      width=layout_w, align=Pango.Alignment.CENTER, vcenter=True)
        return False

    def _draw_chevron(self, cr, cx, cy, s, left: bool, hover: bool) -> None:
        if hover:
            cr.arc(cx, cy, s * 1.75, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.12)
            cr.fill()
        d = -1 if left else 1
        cr.move_to(cx - d * s * 0.45, cy - s)
        cr.line_to(cx + d * s * 0.55, cy)
        cr.line_to(cx - d * s * 0.45, cy + s)
        cr.set_line_width(max(2.0, s * 0.34))
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_source_rgba(1, 1, 1, 0.95 if hover else 0.45)
        cr.stroke()

    def _draw_cell(self, cr, d: dt.date, x0: float, y0: float, c: float, is_today: bool) -> None:
        look = getattr(self, "_cur_look", None) or self._look()
        sh = look["shadow"]
        in_month = d.month == self.month
        inset = max(1.5, c * 0.04)
        x, y, s = x0 + inset, y0 + inset, c - 2 * inset
        r = c * 0.12
        entries = self.entries_for(d)
        cats: List[str] = []
        for e in entries:
            if e.get("category") not in cats:
                cats.append(e.get("category"))

        cr.save()
        rounded_rect(cr, x, y, s, s, r)
        cr.clip()
        cr.set_source_rgba(1, 1, 1, (0.06 if in_month else 0.025) * look["fill"])
        cr.paint()
        if cats:  # one colour fills the cell; several colours -> vertical stripes
            a = (0.62 if in_month else 0.24) * look["cat"]
            sw = s / len(cats)
            for i, name in enumerate(cats):
                cr.rectangle(x + i * sw, y, sw + 0.5, s)
                rr, gg, bb = self.store.color(name)
                cr.set_source_rgba(rr, gg, bb, a)
                cr.fill()
        if self.hover == date_key(d):
            cr.set_source_rgba(1, 1, 1, 0.12)
            cr.paint()
        cr.restore()

        na = self._ta(0.97 if in_month else 0.33, look, primary=in_month)
        num_rgb = (1.0, 0.86, 0.86) if (d.weekday() >= 5 and in_month and not cats) else (1, 1, 1)
        fs = c * 0.22
        show_text(cr, str(d.day), x + s * 0.09, y + s * 0.05, fs, (*num_rgb, na), bold=in_month or is_today,
                  shadow=sh if in_month else 0.0)

        if entries:
            ta = self._ta(0.95 if in_month else 0.4, look, primary=in_month)
            small = max(7.0, c * 0.125)
            if c >= 58:
                max_lines = 3 if c >= 105 else (2 if c >= 72 else 1)
                ty = y + s * 0.05 + fs * 1.3
                shown = 0
                for e in entries:
                    if shown >= max_lines or ty + small * 1.25 > y + s - small * 0.2:
                        break
                    txt = (f"{e['time']} " if e.get("time") else "") + e.get("text", "")
                    show_text(cr, txt, x + s * 0.08, ty, small, (1, 1, 1, ta), width=s * 0.86,
                              shadow=sh if in_month else 0.0)
                    ty += small * 1.28
                    shown += 1
                rest = len(entries) - shown
                if rest > 0:
                    show_text(cr, f"+{rest}", x + s * 0.08, y + s - small * 1.45, small,
                              (1, 1, 1, ta * 0.85), bold=True, width=s * 0.84, align=Pango.Alignment.RIGHT)
            else:
                show_text(cr, str(len(entries)), x + s * 0.08, y + s - small * 1.4, small,
                          (1, 1, 1, ta), bold=True, width=s * 0.84, align=Pango.Alignment.RIGHT)

        if self.store.is_important(d):
            mark_a = 1.0 if in_month else 0.5
            rounded_rect(cr, x + 0.8, y + 0.8, s - 1.6, s - 1.6, max(2.0, r - 1))
            cr.set_line_width(max(2.0, c * 0.05))
            cr.set_source_rgba(*IMPORTANT_RGB, 0.95 * mark_a)
            cr.stroke()

        if is_today:
            rounded_rect(cr, x + 1, y + 1, s - 2, s - 2, r)
            cr.set_line_width(max(2.0, c * 0.035))
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.stroke()

        if self.store.is_important(d):
            draw_important_mark(cr, x, y, s, 1.0 if in_month else 0.5)
            show_text(cr, str(d.day), x + s * 0.09, y + s * 0.05, fs, (*num_rgb, na),
                      bold=True, shadow=sh if in_month else 0.0)


# --------------------------------------------------------------- day dialog
class DayDialog(Gtk.Dialog):
    def __init__(self, app: CalendarWidget, day: dt.date) -> None:
        title = f"{day.strftime('%A')} {day.day} {day.strftime('%B %Y')}"
        super().__init__(title=title, transient_for=app, modal=False)
        self.app = app
        self.day = day
        self.editing: Optional[dict] = None
        self.busy = False
        self._span_locked = False
        self.span_anchor_date = day
        self.set_default_size(480, 560)
        self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.connect("response", lambda *_: self.destroy())
        self.connect("key-press-event", self._on_key)

        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        head = Gtk.Label(xalign=0)
        head.set_markup(f"<b><big>{GLib.markup_escape_text(title)}</big></b>")
        box.pack_start(head, False, False, 0)
        imp_row = Gtk.Box(spacing=6)
        self.important = Gtk.CheckButton(label="Important")
        self.important.set_tooltip_text("Put a red ! across this day")
        self.important.set_active(self.app.store.is_important(day))
        self.important.connect("toggled", self._on_important)
        bang = Gtk.Label()
        bang.set_markup("<span foreground='#DC0D14' weight='bold' size='x-large'>!</span>")
        imp_row.pack_start(self.important, False, False, 0)
        imp_row.pack_start(bang, False, False, 0)
        box.pack_start(imp_row, False, False, 0)
        self.sync_label = Gtk.Label(xalign=0)
        self.sync_label.get_style_context().add_class("dim-label")
        box.pack_start(self.sync_label, False, False, 0)

        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_vexpand(True)
        sc.set_min_content_height(150)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        sc.add(self.listbox)
        box.pack_start(sc, True, True, 0)
        box.pack_start(Gtk.Separator(), False, False, 2)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.form_title = Gtk.Label(xalign=0)
        grid.attach(self.form_title, 0, 0, 2, 1)
        grid.attach(Gtk.Label(label="Entry", xalign=1), 0, 1, 1, 1)
        self.text = Gtk.Entry()
        self.text.set_hexpand(True)
        self.text.set_placeholder_text("e.g. Dentist, team meeting…")
        self.text.connect("activate", lambda *_: self._submit())
        grid.attach(self.text, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="Category", xalign=1), 0, 2, 1, 1)
        self.combo = Gtk.ComboBoxText()
        grid.attach(self.combo, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label="Time", xalign=1), 0, 3, 1, 1)
        self.time = Gtk.Entry()
        self.time.set_placeholder_text("HH:MM (optional, blank = all day)")
        self.time.connect("activate", lambda *_: self._submit())
        grid.attach(self.time, 1, 3, 1, 1)
        grid.attach(Gtk.Label(label="Days", xalign=1), 0, 4, 1, 1)
        span_box = Gtk.Box(spacing=8)
        self.days = Gtk.SpinButton.new_with_range(1, MAX_SPAN_DAYS, 1)
        self.days.set_digits(0)
        self.days.set_tooltip_text(
            "Block several days with one entry, such as a week of travel. "
            "The count starts on the first day of the entry.")
        self.span_label = Gtk.Label(xalign=0)
        self.span_label.set_hexpand(True)
        self.span_label.set_line_wrap(True)
        span_box.pack_start(self.days, False, False, 0)
        span_box.pack_start(self.span_label, True, True, 0)
        grid.attach(span_box, 1, 4, 1, 1)
        presets = Gtk.Box(spacing=6)
        self.preset_btns: List[Gtk.Widget] = []
        for label, kind in (("To Sunday", "sunday"), ("7 days", 7), ("14 days", 14)):
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _w, kind=kind: self._apply_preset(kind))
            presets.pack_start(button, False, False, 0)
            self.preset_btns.append(button)
        self.until_btn = Gtk.Button(label="Until…")
        self.until_btn.set_tooltip_text("Double-click the last day on the calendar")
        self.until_btn.connect("clicked", self._open_until)
        presets.pack_start(self.until_btn, False, False, 0)
        self.preset_btns.append(self.until_btn)
        grid.attach(presets, 1, 5, 1, 1)
        self.until_pop = Gtk.Popover(relative_to=self.until_btn)
        self.until_cal = Gtk.Calendar()
        self.until_cal.connect("day-selected-double-click", self._until_picked)
        self.until_pop.add(self.until_cal)
        self.days.connect("value-changed", self._sync_span_widgets)
        box.pack_start(grid, False, False, 0)

        row = Gtk.Box(spacing=6)
        self.submit_btn = Gtk.Button(label="Add")
        self.submit_btn.get_style_context().add_class("suggested-action")
        self.submit_btn.connect("clicked", lambda *_: self._submit())
        self.cancel_btn = Gtk.Button(label="Cancel edit")
        self.cancel_btn.connect("clicked", lambda *_: self._reset_form())
        self.cancel_btn.set_no_show_all(True)
        cats_btn = Gtk.Button(label="Manage categories…")
        cats_btn.connect("clicked", lambda *_: self.app.open_categories(self))
        row.pack_start(self.submit_btn, False, False, 0)
        row.pack_start(self.cancel_btn, False, False, 0)
        row.pack_end(cats_btn, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        box.pack_start(self.status, False, False, 0)

        self._fill_combo()
        self._reset_form()
        self.reload()

    def after_show(self) -> None:
        self.cancel_btn.hide()
        self.text.grab_focus()

    def _on_important(self, btn) -> None:
        self.app.store.set_important(self.day, btn.get_active())
        self.app.area.queue_draw()

    def _on_key(self, _w, event) -> bool:
        if Gdk.keyval_name(event.keyval) == "Escape":
            if self.editing is not None:
                self._reset_form()
            else:
                self.destroy()
            return True
        return False

    def _fill_combo(self, select: Optional[str] = None) -> None:
        cur = select or self.combo.get_active_text()
        self.combo.remove_all()
        names = self.app.store.names()
        for n in names:
            self.combo.append_text(n)
        want = cur if cur in names else (names[0] if names else None)
        if want is not None:
            self.combo.set_active(names.index(want))

    def reload(self, categories: bool = False) -> None:
        if categories:
            self._fill_combo()
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        app = self.app
        if app.can_write_remote():
            online = any(c["uid"] == app.selected_uid() and c["remote"] for c in app.calendars)
            where = "synced online" if online else "Evolution calendar on this computer"
            self.sync_label.set_text(f"New entries go to: {app.calendar_name()} ({where})")
        elif app.selected_uid() and app.eds is not None:
            self.sync_label.set_text(f"{app.calendar_name()} not connected — new entries saved locally")
        else:
            self.sync_label.set_text("Local only — entries saved on this computer")
        entries = app.entries_for(self.day)
        if not entries:
            lbl = Gtk.Label(label="No entries yet. Add one below.", xalign=0)
            lbl.get_style_context().add_class("dim-label")
            lbl.set_margin_top(8)
            lbl.set_margin_start(6)
            self.listbox.add(lbl)
        for e in entries:
            self.listbox.add(self._row(e))
        self.listbox.show_all()

    def _row(self, e: dict) -> Gtk.Widget:
        row = Gtk.Box(spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(row, f"set_margin_{side}")(4)
        sw = Gtk.DrawingArea()
        sw.set_size_request(14, 14)
        sw.set_valign(Gtk.Align.CENTER)
        rgb = self.app.store.color(e.get("category", OTHER))

        def draw_swatch(_w, cr, rgb=rgb):
            rounded_rect(cr, 0, 0, 14, 14, 3)
            cr.set_source_rgb(*rgb)
            cr.fill()
            return False

        sw.connect("draw", draw_swatch)
        row.pack_start(sw, False, False, 0)
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        t = Gtk.Label(xalign=0)
        t.set_line_wrap(True)
        t.set_max_width_chars(34)
        prefix = f"<b>{e['time']}</b>  " if e.get("time") else ""
        t.set_markup(prefix + GLib.markup_escape_text(e.get("text", "")))
        vb.pack_start(t, False, False, 0)
        meta = e.get("category", OTHER)
        n = span_days(e)
        if n > 1:
            start = key_date(e.get("start") or "") or self.day
            end = start + dt.timedelta(days=n - 1)
            meta += f" · {start.strftime('%-d %b')} to {end.strftime('%-d %b')} ({n} days)"
        if e.get("remote"):
            meta += f" · {self.app.calendar_name()}"
            if e.get("recurring"):
                meta += " · repeats"
        else:
            meta += " · this computer only"
        m = Gtk.Label(label=meta, xalign=0)
        m.get_style_context().add_class("dim-label")
        vb.pack_start(m, False, False, 0)
        row.pack_start(vb, True, True, 0)
        edit = Gtk.Button(label="Edit")
        edit.connect("clicked", lambda *_: self._start_edit(e))
        dele = Gtk.Button(label="Delete")
        dele.connect("clicked", lambda *_: self._delete(e))
        if e.get("recurring"):
            edit.set_sensitive(False)
            edit.set_tooltip_text("Repeating event — edit it in Google Calendar")
            dele.set_tooltip_text("Delete this occurrence only")
        row.pack_end(dele, False, False, 0)
        row.pack_end(edit, False, False, 0)
        return row

    def _span_anchor(self) -> dt.date:
        return self.span_anchor_date or self.day

    def _set_span_locked(self, locked: bool) -> None:
        self._span_locked = locked
        sens = not locked and not self.busy
        self.days.set_sensitive(sens)
        for button in self.preset_btns:
            button.set_sensitive(sens)

    def _sync_span_widgets(self, *_a) -> None:
        n = max(1, int(round(self.days.get_value())))
        multi = n > 1
        self.time.set_sensitive(not multi and not self.busy)
        self.span_label.set_text(span_range_text(self._span_anchor(), n))
        if self.editing is None and not self.busy:
            self.submit_btn.set_label(f"Add {n} days" if multi else "Add")

    def _apply_preset(self, kind) -> None:
        if kind == "sunday":
            n = days_through_sunday(self._span_anchor())
        else:
            n = int(kind)
        self.days.set_value(n)

    def _open_until(self, *_a) -> None:
        end = self._span_anchor() + dt.timedelta(days=max(1, int(round(self.days.get_value()))) - 1)
        self.until_cal.select_month(end.month - 1, end.year)
        self.until_cal.select_day(end.day)
        self.until_pop.show_all()
        self.until_pop.popup()

    def _until_picked(self, cal) -> None:
        year, month, day = cal.get_date()
        end = dt.date(year, month + 1, day)
        anchor = self._span_anchor()
        if end < anchor:
            self.status.set_text("Pick an end date on or after the start.")
            return
        n = (end - anchor).days + 1
        if n > MAX_SPAN_DAYS:
            self.status.set_text(f"A block can cover up to {MAX_SPAN_DAYS} days.")
            n = MAX_SPAN_DAYS
        else:
            self.status.set_text("")
        self.days.set_value(n)
        self.until_pop.popdown()

    def _reset_form(self) -> None:
        self.editing = None
        self.span_anchor_date = self.day
        self.form_title.set_markup("<b>Add entry</b>")
        self.submit_btn.set_label("Add")
        self.cancel_btn.hide()
        self.text.set_text("")
        self.time.set_text("")
        self.days.set_value(1)
        self._set_span_locked(False)
        self._sync_span_widgets()
        self.status.set_text("")

    def _start_edit(self, e: dict) -> None:
        self.editing = e
        self.span_anchor_date = key_date(e.get("start") or "") or self.day
        self.form_title.set_markup("<b>Edit entry</b>")
        self.submit_btn.set_label("Save changes")
        self.cancel_btn.show()
        self.text.set_text(e.get("text", ""))
        self.time.set_text("" if span_days(e) > 1 else e.get("time", ""))
        self.days.set_value(span_days(e))
        self._fill_combo(select=e.get("category", OTHER))
        self._set_span_locked(bool(e.get("recurring")))
        self._sync_span_widgets()
        self.status.set_text("")
        self.text.grab_focus()

    def _set_busy(self, busy: bool, msg: str = "") -> None:
        self.busy = busy
        self.submit_btn.set_sensitive(not busy)
        self.listbox.set_sensitive(not busy)
        self._set_span_locked(self._span_locked)
        self._sync_span_widgets()
        self.status.set_text(msg)

    def _done(self, note: Optional[str]) -> None:
        if self.get_window() is None:  # dialog closed meanwhile
            return
        self._set_busy(False)
        failed = note is not None and note.startswith("Couldn't") and self.editing is not None
        if not failed:
            self._reset_form()
        self.reload()
        if note:
            self.status.set_markup(f"<span foreground='#c0392b'>{GLib.markup_escape_text(note)}</span>")

    def _submit(self) -> None:
        if self.busy:
            return
        text = self.text.get_text().strip()
        if not text:
            self.status.set_text("Type something for the entry first.")
            return
        hhmm = parse_hhmm(self.time.get_text())
        if hhmm is None:
            self.status.set_markup(
                "<span foreground='#c0392b'>Time must be HH:MM (24h), or blank for all day.</span>")
            return
        cat = self.combo.get_active_text() or OTHER
        ndays = max(1, int(round(self.days.get_value())))
        if ndays > 1:
            hhmm = ""
        remote = self.app.can_write_remote() or bool(self.editing and self.editing.get("remote"))
        self._set_busy(True, f"Saving to {self.app.calendar_name()}…" if remote else "")
        if self.editing is not None:
            self.app.update_entry(self.day, self.editing, text, cat, hhmm, self._done, ndays)
        else:
            self.app.add_entry(self.day, text, cat, hhmm, self._done, ndays)

    def _delete(self, e: dict) -> None:
        if self.busy:
            return
        what = "this occurrence of " if e.get("recurring") else ""
        where = f" from {self.app.calendar_name()}" if e.get("remote") else ""
        n = span_days(e)
        if n > 1 and not e.get("recurring"):
            prompt = f"Remove “{e.get('text', '')}” from all {n} days?"
        else:
            prompt = f"Delete {what}“{e.get('text', '')}”{where}?"
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                text=prompt)
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        if self.editing is not None and self.editing.get("id") == e.get("id"):
            self._reset_form()
        self._set_busy(True, "Deleting…" if e.get("remote") else "")
        self.app.delete_entry(self.day, e, self._done)


# ---------------------------------------------------------- category dialog
class CategoryDialog(Gtk.Dialog):
    def __init__(self, parent, store: Store) -> None:
        super().__init__(title="Manage categories", transient_for=parent, modal=True)
        self.store = store
        self.set_default_size(420, 470)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = self.add_button("Save", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        info = Gtk.Label(xalign=0)
        info.set_markup("Rename, recolour, add or remove categories.\n"
                        "<small>Synced events store the category in the iCal CATEGORIES field.\n"
                        "Events without a known category (e.g. added on another device) show as 'Other'.</small>")
        box.pack_start(info, False, False, 0)
        sc = Gtk.ScrolledWindow()
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc.set_vexpand(True)
        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sc.add(self.rows_box)
        box.pack_start(sc, True, True, 0)
        add = Gtk.Button(label="Add category")
        add.connect("clicked", lambda *_: self._add_row(None, "", "#14B8A6", focus=True))
        box.pack_start(add, False, False, 0)
        self.err = Gtk.Label(xalign=0)
        box.pack_start(self.err, False, False, 0)
        self.rows: List[dict] = []
        for c in store.categories:
            self._add_row(c["name"], c["name"], c["color"])
        self.show_all()

    def _add_row(self, orig, name, color, focus=False) -> None:
        hb = Gtk.Box(spacing=6)
        entry = Gtk.Entry()
        entry.set_text(name)
        entry.set_hexpand(True)
        rgba = Gdk.RGBA()
        rgba.parse(color)
        cb = Gtk.ColorButton.new_with_rgba(rgba)
        rm = Gtk.Button(label="Remove")
        row = {"orig": orig, "entry": entry, "color": cb, "box": hb}
        if orig == OTHER:
            entry.set_sensitive(False)
            rm.set_sensitive(False)
            rm.set_tooltip_text("'Other' is used for events without a category")
        rm.connect("clicked", lambda *_: self._remove_row(row))
        hb.pack_start(entry, True, True, 0)
        hb.pack_start(cb, False, False, 0)
        hb.pack_start(rm, False, False, 0)
        self.rows_box.pack_start(hb, False, False, 0)
        self.rows.append(row)
        hb.show_all()
        if focus:
            entry.grab_focus()

    def _remove_row(self, row) -> None:
        self.rows.remove(row)
        row["box"].destroy()

    def validate(self) -> bool:
        names = [r["entry"].get_text().strip() for r in self.rows]
        err = None
        if any(not n for n in names):
            err = "Category names can't be empty."
        elif len({n.lower() for n in names}) != len(names):
            err = "Category names must be unique."
        self.err.set_markup(f"<span foreground='#c0392b'>{err}</span>" if err else "")
        return err is None

    def apply(self) -> None:
        old = {c["name"]: c for c in self.store.categories}
        new_cats: List[dict] = []
        renames: Dict[str, str] = {}
        for r in self.rows:
            name = r["entry"].get_text().strip()
            color = rgba_to_hex(r["color"].get_rgba())
            aliases: List[str] = []
            if r["orig"] and r["orig"] in old:
                aliases = list(old[r["orig"]].get("aliases", []))
                if r["orig"] != name:
                    renames[r["orig"]] = name
                    aliases.append(r["orig"])  # old name still matches synced events
            aliases = [a for a in dict.fromkeys(aliases) if a.lower() != name.lower()]
            new_cats.append({"name": name, "color": color, "aliases": aliases})
        self.store.categories = new_cats
        self.store._ensure_other()
        valid = set(self.store.names())
        for m in (self.store.entries, self.store.cache):
            for lst in m.values():
                for e in lst:
                    cat = renames.get(e.get("category"), e.get("category"))
                    e["category"] = cat if cat in valid else OTHER


def main() -> None:
    win = CalendarWidget()
    win.show_all()
    secs = os.environ.get("CALENDAR_WIDGET_SELFTEST_SECONDS")
    if secs:  # used by the test harness: quit cleanly after N seconds
        GLib.timeout_add(int(float(secs) * 1000), win.quit)
    Gtk.main()


if __name__ == "__main__":
    main()
