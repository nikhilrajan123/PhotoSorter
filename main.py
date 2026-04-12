"""
PhotoSorter Pro — Android
Kivy-based mobile app for sorting, renaming & compressing
photos/videos directly on your Android phone.

Tabs:
  1. Sort & Rename  — Year / Month / Date+City
  2. Video Compressor
  3. Image Converter  (HEIC → JPG etc.)

Install on phone via:
  buildozer android debug deploy run
  OR see BUILD.md
"""

import os, re, json, shutil, time, threading, subprocess
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# ── Kivy config must be set BEFORE importing kivy ────────────────────
os.environ.setdefault("KIVY_NO_ENV_CONFIG", "1")

from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout
from kivy.uix.tabbedpanel import TabbedPanel, TabbedPanelItem
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.progressbar import ProgressBar
from kivy.uix.spinner import Spinner
from kivy.uix.checkbox import CheckBox
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.clock import Clock, mainthread
from kivy.metrics import dp
from kivy.properties import StringProperty, NumericProperty, BooleanProperty
from kivy.core.window import Window
from kivy.utils import get_color_from_hex

# ── Colours ───────────────────────────────────────────────────────────
BG       = get_color_from_hex("#0f0f0f")
CARD     = get_color_from_hex("#1a1a1a")
ACCENT   = get_color_from_hex("#f0c040")
FG       = get_color_from_hex("#f0ede6")
MUTED    = get_color_from_hex("#888880")
GREEN    = get_color_from_hex("#6abf6a")
RED      = get_color_from_hex("#e05555")
BLUE     = get_color_from_hex("#5588cc")
BORDER   = get_color_from_hex("#2a2a2a")

Window.clearcolor = BG

# ── Optional deps ─────────────────────────────────────────────────────
HEIF_OK = False
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_OK = True
except ImportError:
    pass

try:
    from PIL import Image, ImageOps
    from PIL.ExifTags import TAGS, GPSTAGS
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    import requests
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False

# ══════════════════════════════════════════════════════════════════════
#  SHARED LOGIC  (identical to desktop PhotoSorter)
# ══════════════════════════════════════════════════════════════════════

PHOTO_EXT = {".jpg",".jpeg",".png",".heic",".heif",".tiff",".tif",
             ".webp",".dng",".raw",".bmp"}
VIDEO_EXT = {".mp4",".mkv",".mov",".avi",".wmv",".flv",".webm",
             ".m4v",".3gp",".ts",".mts",".m2ts"}
ALL_EXT   = PHOTO_EXT | VIDEO_EXT

MONTHS = ["01 - January","02 - February","03 - March","04 - April",
          "05 - May","06 - June","07 - July","08 - August",
          "09 - September","10 - October","11 - November","12 - December"]

_loc_cache: dict = {}


def get_photo_exif(path: Path) -> dict:
    if not PIL_OK: return {}
    try:
        img = Image.open(path)
        raw = None
        if hasattr(img,"_getexif"): raw = img._getexif()
        if raw is None and hasattr(img,"getexif"): raw = dict(img.getexif())
        return {TAGS.get(t,t): v for t,v in raw.items()} if raw else {}
    except: return {}

def get_photo_gps(exif: dict):
    raw = exif.get("GPSInfo")
    if not raw: return None
    try:
        gps = {GPSTAGS.get(k,k): v for k,v in raw.items()}
    except: return None
    def dec(dms, ref):
        try:
            vals = [x[0]/x[1] if isinstance(x,tuple) and x[1] else float(x) for x in dms]
            d,m,s = vals
            v = d+m/60+s/3600
            return -v if ref in ("S","W") else v
        except: return None
    lat = dec(gps.get("GPSLatitude",[]), gps.get("GPSLatitudeRef","N"))
    lon = dec(gps.get("GPSLongitude",[]), gps.get("GPSLongitudeRef","E"))
    return (lat,lon) if lat is not None and lon is not None else None

def get_photo_date(path: Path, exif: dict) -> datetime:
    s = exif.get("DateTimeOriginal") or exif.get("DateTime")
    if s:
        try: return datetime.strptime(str(s), "%Y:%m:%d %H:%M:%S")
        except: pass
    return datetime.fromtimestamp(path.stat().st_mtime)

def find_ffprobe():
    for exe in ["ffprobe", "/data/data/com.termux/files/usr/bin/ffprobe"]:
        try:
            subprocess.run([exe,"-version"], capture_output=True, timeout=5)
            return exe
        except: pass
    return ""

def find_ffmpeg():
    for exe in ["ffmpeg", "/data/data/com.termux/files/usr/bin/ffmpeg"]:
        try:
            subprocess.run([exe,"-version"], capture_output=True, timeout=5)
            return exe
        except: pass
    return ""

def get_video_metadata(path: Path) -> dict:
    probe = find_ffprobe()
    if not probe: return {}
    try:
        r = subprocess.run([probe,"-v","quiet","-print_format","json",
                            "-show_format","-show_streams", str(path)],
                           capture_output=True, timeout=30)
        return json.loads(r.stdout) if r.returncode==0 else {}
    except: return {}

def get_video_gps(meta: dict):
    tags = {}
    tags.update(meta.get("format",{}).get("tags",{}))
    for s in meta.get("streams",[]): tags.update(s.get("tags",{}))
    for key, val in tags.items():
        if not isinstance(val,str): continue
        if any(x in key.lower() for x in ("location","gps","©xyz","coordinates","geo")):
            m = re.match(r"([+-]\d{1,3}\.?\d*)([+-]\d{1,3}\.?\d*)", val.strip())
            if m:
                try:
                    lat,lon = float(m.group(1)), float(m.group(2))
                    if -90<=lat<=90 and -180<=lon<=180: return (lat,lon)
                except: pass
    return None

def get_video_date(path: Path, meta: dict) -> datetime:
    tags = {}
    tags.update(meta.get("format",{}).get("tags",{}))
    for s in meta.get("streams",[]): tags.update(s.get("tags",{}))
    for key, val in tags.items():
        if "creation_time" in key.lower() and isinstance(val, str):
            for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ","%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%d %H:%M:%S","%Y-%m-%dT%H:%M:%S"):
                try: return datetime.strptime(val[:19], fmt[:19])
                except: pass
    return datetime.fromtimestamp(path.stat().st_mtime)

def coords_to_city(lat: float, lon: float) -> str:
    if not REQUESTS_OK: return ""
    key = (round(lat,2), round(lon,2))
    if key in _loc_cache: return _loc_cache[key]
    try:
        r = requests.get("https://nominatim.openstreetmap.org/reverse",
                         params={"lat":lat,"lon":lon,"format":"json","zoom":10},
                         headers={"User-Agent":"PhotoSorterAndroid/1.0"}, timeout=8)
        addr = r.json().get("address",{})
        city = (addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("county") or addr.get("state") or "")
        city = city.replace("/","-").replace("\\","-").strip()
        _loc_cache[key] = city
        time.sleep(1)
        return city
    except: return ""

def sanitize(n: str) -> str:
    for ch in r'\/:*?"<>|': n = n.replace(ch,"-")
    return n.strip()

def date_folder_name(dt: datetime, cities: list) -> str:
    base = dt.strftime("%Y-%m-%d")
    if cities:
        loc = ", ".join(cities)
        full = f"{base} {loc}"
        return sanitize(full[:180] if len(full)>180 else full)
    return base

def photo_filename(city: str, dt: datetime, suffix: str) -> str:
    d,t = dt.strftime("%Y-%m-%d"), dt.strftime("%H-%M-%S")
    return sanitize(f"{city} {d} {t}" if city else f"{d} {t}") + suffix.lower()

def video_filename(city: str, dt: datetime, suffix: str) -> str:
    d,t = dt.strftime("%Y-%m-%d"), dt.strftime("%H-%M-%S")
    return sanitize(f"Video {city} {d} {t}" if city else f"Video {d} {t}") + suffix.lower()

def build_dest_dir(base: Path, dt: datetime, cities: list) -> Path:
    return base / str(dt.year) / MONTHS[dt.month-1] / date_folder_name(dt, cities)

def unique_path(dest_dir: Path, filename: str) -> Path:
    dest = dest_dir / filename
    if not dest.exists(): return dest
    stem, suf = Path(filename).stem, Path(filename).suffix
    i = 2
    while True:
        c = dest_dir / f"{stem} ({i}){suf}"
        if not c.exists(): return c
        i += 1


# ══════════════════════════════════════════════════════════════════════
#  UI HELPERS
# ══════════════════════════════════════════════════════════════════════

def card_btn(text, on_press, bg=CARD, fg=ACCENT, size_hint_x=1, height=dp(48)):
    btn = Button(text=text, background_color=bg, color=fg,
                 size_hint_x=size_hint_x, size_hint_y=None, height=height,
                 bold=True, font_size=dp(15))
    btn.bind(on_press=on_press)
    return btn

def section_label(text, color=MUTED):
    return Label(text=text, color=color, size_hint_y=None,
                 height=dp(28), font_size=dp(12), halign="left",
                 text_size=(Window.width-dp(32), None))

def log_label():
    lbl = Label(text="", color=FG, size_hint_y=None, font_size=dp(11),
                halign="left", valign="top", markup=True)
    lbl.bind(texture_size=lbl.setter("size"))
    lbl.bind(width=lambda inst,v: setattr(inst,"text_size",(v,None)))
    return lbl


# ══════════════════════════════════════════════════════════════════════
#  FILE CHOOSER POPUP
# ══════════════════════════════════════════════════════════════════════

class FolderPickerPopup(Popup):
    def __init__(self, callback, start_path="/sdcard", **kwargs):
        super().__init__(**kwargs)
        self.callback = callback
        self.title    = "Select Folder"
        self.size_hint = (0.95, 0.85)

        layout = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(8))

        self.fc = FileChooserListView(
            path=start_path,
            dirselect=True,
            filters=["*/"],
        )
        layout.add_widget(self.fc)

        btn_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        cancel = Button(text="Cancel", background_color=CARD, color=MUTED)
        select = Button(text="Select This Folder", background_color=ACCENT, color=BG)
        cancel.bind(on_press=self.dismiss)
        select.bind(on_press=self._select)
        btn_row.add_widget(cancel)
        btn_row.add_widget(select)
        layout.add_widget(btn_row)

        self.content = layout

    def _select(self, *a):
        path = self.fc.path
        if self.fc.selection:
            path = self.fc.selection[0]
        self.callback(path)
        self.dismiss()


# ══════════════════════════════════════════════════════════════════════
#  TAB 1 — SORT & RENAME
# ══════════════════════════════════════════════════════════════════════

class SortTab(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", spacing=dp(10),
                         padding=dp(12), **kwargs)
        self._src = ""
        self._dst = ""
        self._cancel = False
        self._build()

    def _build(self):
        self.add_widget(Label(text="Sort & Rename",
                              color=ACCENT, bold=True, font_size=dp(20),
                              size_hint_y=None, height=dp(40)))

        info = ("[b]Year / Month / Date+City[/b]\n"
                "Photos: [color=#6abf6a]Kozhikode 2024-03-18 14-23-01.jpg[/color]\n"
                "Videos: [color=#6abf6a]Video Kozhikode 2024-04-01 09-12-34.mp4[/color]")
        self.add_widget(Label(text=info, color=MUTED, markup=True,
                              size_hint_y=None, height=dp(72),
                              font_size=dp(12), halign="left",
                              text_size=(Window.width-dp(32), None)))

        # Source folder
        src_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self._src_lbl = Label(text="Source: not set", color=MUTED,
                              font_size=dp(12), halign="left",
                              text_size=(Window.width*0.65, None))
        src_btn = Button(text="Browse", background_color=CARD, color=ACCENT,
                         size_hint_x=0.3, bold=True)
        src_btn.bind(on_press=lambda *a: self._pick("src"))
        src_row.add_widget(self._src_lbl)
        src_row.add_widget(src_btn)
        self.add_widget(src_row)

        # Dest folder
        dst_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self._dst_lbl = Label(text="Dest:   not set", color=MUTED,
                              font_size=dp(12), halign="left",
                              text_size=(Window.width*0.65, None))
        dst_btn = Button(text="Browse", background_color=CARD, color=ACCENT,
                         size_hint_x=0.3, bold=True)
        dst_btn.bind(on_press=lambda *a: self._pick("dst"))
        dst_row.add_widget(self._dst_lbl)
        dst_row.add_widget(dst_btn)
        self.add_widget(dst_row)

        # Progress
        self._prog = ProgressBar(max=100, value=0,
                                 size_hint_y=None, height=dp(10))
        self.add_widget(self._prog)
        self._status = Label(text="Ready", color=FG, font_size=dp(13),
                             size_hint_y=None, height=dp(28))
        self.add_widget(self._status)

        # Log scroll
        sv = ScrollView(size_hint=(1,1))
        self._log = log_label()
        sv.add_widget(self._log)
        self.add_widget(sv)

        # Buttons
        btn_row = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(8))
        self._start_btn = card_btn("▶  Start Sorting", self._start, ACCENT, BG)
        self._stop_btn  = card_btn("⏹  Stop", self._stop, CARD, RED, 0.35)
        self._stop_btn.disabled = True
        btn_row.add_widget(self._start_btn)
        btn_row.add_widget(self._stop_btn)
        self.add_widget(btn_row)

    def _pick(self, which):
        start = self._src if which=="dst" and self._src else "/sdcard"
        def cb(path):
            if which == "src":
                self._src = path
                self._src_lbl.text = f"Source: ...{path[-30:]}"
                if not self._dst:
                    self._dst = str(Path(path).parent / "Sorted_Photos")
                    self._dst_lbl.text = f"Dest:   ...{self._dst[-30:]}"
            else:
                self._dst = path
                self._dst_lbl.text = f"Dest:   ...{path[-30:]}"
        FolderPickerPopup(callback=cb, start_path=start).open()

    def _start(self, *a):
        if not self._src or not self._dst:
            self._status.text = "⚠ Please select source and destination folders"
            return
        if not Path(self._src).exists():
            self._status.text = "⚠ Source folder not found"
            return
        self._cancel = False
        self._start_btn.disabled = True
        self._stop_btn.disabled  = False
        self._log.text = ""
        threading.Thread(target=self._run,
                         args=(Path(self._src), Path(self._dst)),
                         daemon=True).start()

    def _stop(self, *a):
        self._cancel = True
        self._stop_btn.disabled = True

    @mainthread
    def _update(self, status=None, log=None, prog=None):
        if status: self._status.text = status
        if log:    self._log.text += log + "\n"
        if prog is not None: self._prog.value = prog

    def _run(self, src: Path, dst: Path):
        files = [f for f in src.rglob("*")
                 if f.is_file() and f.suffix.lower() in ALL_EXT]
        total = len(files)
        if not files:
            self._update(status="⚠ No files found"); return

        self._update(status=f"Found {total} files — Pass 1: reading GPS…", prog=0)

        # Pass 1: collect metadata
        file_meta   = {}
        date_cities = defaultdict(dict)

        for i, f in enumerate(files):
            if self._cancel: break
            try:
                is_photo = f.suffix.lower() in PHOTO_EXT
                if is_photo:
                    exif   = get_photo_exif(f)
                    dt     = get_photo_date(f, exif)
                    coords = get_photo_gps(exif)
                else:
                    meta   = get_video_metadata(f)
                    dt     = get_video_date(f, meta)
                    coords = get_video_gps(meta)
                city = coords_to_city(*coords) if coords else ""
                file_meta[f] = (dt, city, is_photo)
                date_key = (dt.year, dt.month, dt.strftime("%Y-%m-%d"))
                if city: date_cities[date_key][city] = None
            except Exception as ex:
                file_meta[f] = None
                self._update(log=f"⚠ {f.name}: {ex}")
            self._update(prog=int((i+1)/total*50))

        # Pass 2: move & rename
        self._update(status="Pass 2: moving files…")
        moved = errors = 0

        for i, f in enumerate(files):
            if self._cancel: break
            info = file_meta.get(f)
            if not info:
                errors += 1; continue
            try:
                dt, city, is_photo = info
                date_key  = (dt.year, dt.month, dt.strftime("%Y-%m-%d"))
                cities    = list(date_cities[date_key].keys())
                dest_dir  = build_dest_dir(dst, dt, cities)
                dest_dir.mkdir(parents=True, exist_ok=True)
                fname = (photo_filename(city, dt, f.suffix) if is_photo
                         else video_filename(city, dt, f.suffix))
                dest  = unique_path(dest_dir, fname)
                shutil.move(str(f), str(dest))
                icon = "📷" if is_photo else "🎬"
                self._update(
                    log=f"{icon} {f.name}\n  → {dest.name} [{city or 'no GPS'}]",
                    prog=50+int((i+1)/total*50))
                moved += 1
            except Exception as ex:
                self._update(log=f"✗ {f.name}: {ex}")
                errors += 1

        status = ("Stopped" if self._cancel
                  else f"✓ Done — {moved} sorted, {errors} error(s)")
        self._update(status=status, prog=100)
        self._start_btn.disabled = False
        self._stop_btn.disabled  = True


# ══════════════════════════════════════════════════════════════════════
#  TAB 2 — VIDEO COMPRESSOR
# ══════════════════════════════════════════════════════════════════════

PRESETS_ANDROID = {
    "High quality (H.265)":  ("hevc","24","original"),
    "Balanced (H.265)":      ("hevc","28","original"),
    "Space saver (720p)":    ("hevc","32","1280:720"),
    "Maximum (H.264 720p)":  ("h264","32","1280:720"),
}

class VideoCompressorTab(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", spacing=dp(10),
                         padding=dp(12), **kwargs)
        self._folder  = ""
        self._cancel  = False
        self._proc    = None
        self._entries = []
        self._build()

    def _build(self):
        self.add_widget(Label(text="Video Compressor", color=ACCENT,
                              bold=True, font_size=dp(20),
                              size_hint_y=None, height=dp(40)))

        ff = find_ffmpeg()
        ff_color = GREEN if ff else RED
        ff_text  = f"✓ ffmpeg found: {ff}" if ff else "✗ ffmpeg not found — install via Termux"
        self.add_widget(Label(text=ff_text, color=ff_color,
                              font_size=dp(12), size_hint_y=None, height=dp(28)))

        # Folder picker
        fold_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self._fold_lbl = Label(text="Folder: not set", color=MUTED,
                               font_size=dp(12),
                               text_size=(Window.width*0.65, None), halign="left")
        fold_btn = Button(text="Browse", background_color=CARD, color=ACCENT,
                          size_hint_x=0.3, bold=True)
        fold_btn.bind(on_press=lambda *a: self._pick_folder())
        fold_row.add_widget(self._fold_lbl)
        fold_row.add_widget(fold_btn)
        self.add_widget(fold_row)

        # Preset spinner
        preset_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        preset_row.add_widget(Label(text="Preset:", color=MUTED,
                                    size_hint_x=0.3, font_size=dp(13)))
        self._preset = Spinner(
            text=list(PRESETS_ANDROID.keys())[1],
            values=list(PRESETS_ANDROID.keys()),
            background_color=CARD, color=FG,
            size_hint_x=0.7, font_size=dp(13))
        preset_row.add_widget(self._preset)
        self.add_widget(preset_row)

        # Load + start/stop
        action_row = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(8))
        load_btn = Button(text="📂 Load Videos", background_color=BLUE,
                          color=FG, bold=True, size_hint_x=0.35, font_size=dp(14))
        load_btn.bind(on_press=self._load)
        self._comp_btn = Button(text="▶ Compress", background_color=ACCENT,
                                color=BG, bold=True, font_size=dp(15))
        self._comp_btn.bind(on_press=self._start)
        self._stop_btn = Button(text="⏹", background_color=CARD, color=RED,
                                size_hint_x=0.2, bold=True, font_size=dp(16))
        self._stop_btn.bind(on_press=self._stop)
        self._stop_btn.disabled = True
        action_row.add_widget(load_btn)
        action_row.add_widget(self._comp_btn)
        action_row.add_widget(self._stop_btn)
        self.add_widget(action_row)

        # Progress
        self._prog = ProgressBar(max=100, value=0,
                                 size_hint_y=None, height=dp(10))
        self.add_widget(self._prog)
        self._status = Label(text="Ready", color=FG, font_size=dp(13),
                             size_hint_y=None, height=dp(28))
        self.add_widget(self._status)

        # Video list
        self._list_sv = ScrollView(size_hint=(1,1))
        self._list_grid = GridLayout(cols=1, spacing=dp(2),
                                     size_hint_y=None)
        self._list_grid.bind(minimum_height=self._list_grid.setter("height"))
        self._list_sv.add_widget(self._list_grid)
        self.add_widget(self._list_sv)

    def _pick_folder(self):
        def cb(path):
            self._folder = path
            self._fold_lbl.text = f"Folder: ...{path[-35:]}"
        FolderPickerPopup(callback=cb).open()

    def _load(self, *a):
        if not self._folder:
            self._status.text = "⚠ Browse a folder first"; return
        p = Path(self._folder)
        if not p.exists():
            self._status.text = "⚠ Folder not found"; return
        self._entries = sorted(
            [f for f in p.rglob("*")
             if f.is_file() and f.suffix.lower() in VIDEO_EXT
             and "_compressed" not in f.stem],
            key=lambda x: str(x).lower())
        if not self._entries:
            self._status.text = "⚠ No videos found"; return
        self._render_list()
        self._status.text = f"Loaded {len(self._entries)} video(s)"

    def _render_list(self):
        self._list_grid.clear_widgets()
        for f in self._entries:
            try: kb = f.stat().st_size//1024
            except: kb = 0
            sz = f"{kb:,}KB" if kb<1024 else f"{kb/1024:.1f}MB"
            row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(4))
            name_lbl = Label(text=f.name, color=FG, font_size=dp(11),
                             halign="left", text_size=(Window.width*0.5, None))
            size_lbl = Label(text=sz, color=MUTED, font_size=dp(11),
                             size_hint_x=0.2)
            stat_lbl = Label(text="—", color=MUTED, font_size=dp(11),
                             size_hint_x=0.3)
            row.add_widget(name_lbl)
            row.add_widget(size_lbl)
            row.add_widget(stat_lbl)
            row._stat_lbl = stat_lbl
            row._path     = f
            self._list_grid.add_widget(row)

    def _get_row(self, path):
        for row in self._list_grid.children:
            if hasattr(row, "_path") and row._path == path:
                return row
        return None

    @mainthread
    def _update_row(self, path, text, color=GREEN):
        row = self._get_row(path)
        if row: row._stat_lbl.text = text; row._stat_lbl.color = color

    @mainthread
    def _update(self, status=None, prog=None):
        if status: self._status.text = status
        if prog is not None: self._prog.value = prog

    def _start(self, *a):
        if not find_ffmpeg():
            self._status.text = "⚠ ffmpeg not found. Install: pkg install ffmpeg"
            return
        if not self._entries:
            self._status.text = "⚠ Load videos first"; return
        self._cancel = False
        self._comp_btn.disabled = True
        self._stop_btn.disabled = False
        threading.Thread(target=self._run, daemon=True).start()

    def _stop(self, *a):
        self._cancel = True
        if self._proc:
            try: self._proc.terminate()
            except: pass
        self._stop_btn.disabled = True

    def _run(self):
        ff     = find_ffmpeg()
        preset = self._preset.text
        codec, crf, scale = PRESETS_ANDROID[preset]
        total  = len(self._entries)
        done   = saved_total = 0

        for i, vid in enumerate(self._entries):
            if self._cancel: break
            if not vid.exists():
                done += 1; continue

            orig_mb = vid.stat().st_size / (1024*1024)
            self._update(status=f"[{i+1}/{total}] Checking {vid.name}…",
                         prog=int(i/total*100))
            self._update_row(vid, "checking…", MUTED)

            # Sample test
            sample_out = vid.parent / f"__sample_{vid.stem}.mp4"
            sample_secs = max(3.0, min(15.0, orig_mb * 8))  # rough estimate
            cmd_sample  = [ff,"-i",str(vid),"-t",str(sample_secs),
                           "-c:v","libx264","-crf",crf,"-preset","fast",
                           "-c:a","aac","-b:a","128k","-y",str(sample_out)]
            try:
                subprocess.run(cmd_sample, capture_output=True, timeout=120)
                if sample_out.exists():
                    orig_slice = orig_mb * (sample_secs / max(orig_mb*8, 1))
                    comp_slice = sample_out.stat().st_size / (1024*1024)
                    sample_out.unlink(missing_ok=True)
                    if comp_slice >= orig_slice:
                        self._update_row(vid, "already optimal", MUTED)
                        done += 1; continue
            except:
                try: sample_out.unlink(missing_ok=True)
                except: pass

            # Full compression
            out_file = vid.parent / f"{vid.stem}_compressed.mp4"
            ct = 2
            while out_file.exists():
                out_file = vid.parent / f"{vid.stem}_compressed_{ct}.mp4"; ct += 1

            self._update(status=f"[{i+1}/{total}] Compressing {vid.name}…")
            self._update_row(vid, "compressing…", BLUE)

            vc    = "libx265" if codec=="hevc" else "libx264"
            extra = ["-tag:v","hvc1"] if codec=="hevc" else []
            cmd   = [ff,"-i",str(vid),"-c:v",vc,"-crf",crf,"-preset","medium",
                     "-c:a","aac","-b:a","128k","-map_metadata","0",
                     "-movflags","+faststart+use_metadata_tags"]+extra
            if scale != "original":
                cmd += ["-vf",f"scale={scale}:force_original_aspect_ratio=decrease"]
            cmd += ["-y",str(out_file)]

            ok = False
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL)
                self._proc.wait(timeout=7200)
                ok = self._proc.returncode==0 and out_file.exists()
                # H.265 fallback
                if not ok and codec=="hevc" and not self._cancel:
                    cmd2 = [ff,"-i",str(vid),"-c:v","libx264","-crf","28",
                            "-preset","medium","-c:a","aac","-b:a","128k",
                            "-map_metadata","0","-movflags","+faststart"]+["-y",str(out_file)]
                    self._proc = subprocess.Popen(cmd2, stdout=subprocess.DEVNULL,
                                                  stderr=subprocess.DEVNULL)
                    self._proc.wait(timeout=7200)
                    ok = self._proc.returncode==0 and out_file.exists()
            except: ok=False
            finally: self._proc=None

            if ok:
                new_mb = out_file.stat().st_size/(1024*1024)
                if new_mb >= orig_mb:
                    try: out_file.unlink()
                    except: pass
                    self._update_row(vid, "larger—skipped", MUTED)
                else:
                    pct = int((1-new_mb/orig_mb)*100)
                    saved_total += orig_mb-new_mb
                    self._update_row(vid, f"✓ {pct}% saved", GREEN)
            else:
                if out_file.exists():
                    try: out_file.unlink()
                    except: pass
                self._update_row(vid, "error", RED)

            done += 1
            self._update(prog=int((i+1)/total*100))

        status = (f"Stopped" if self._cancel
                  else f"✓ Done — {done} processed, ~{saved_total:.1f}MB freed")
        self._update(status=status, prog=100)
        self._comp_btn.disabled = False
        self._stop_btn.disabled = True


# ══════════════════════════════════════════════════════════════════════
#  TAB 3 — IMAGE CONVERTER
# ══════════════════════════════════════════════════════════════════════

IMG_EXT = {".heic",".heif",".png",".bmp",".tiff",".tif",
           ".webp",".jpg",".jpeg",".raw",".dng"}

class ImageConverterTab(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", spacing=dp(10),
                         padding=dp(12), **kwargs)
        self._folder  = ""
        self._entries = []
        self._cancel  = False
        self._build()

    def _build(self):
        self.add_widget(Label(text="Image Converter & Compressor",
                              color=ACCENT, bold=True, font_size=dp(18),
                              size_hint_y=None, height=dp(40)))

        heif_col = GREEN if HEIF_OK else RED
        heif_txt = "✓ pillow-heif — HEIC/HEIF fully supported" if HEIF_OK else \
                   "✗ pillow-heif missing — pip install pillow-heif"
        self.add_widget(Label(text=heif_txt, color=heif_col,
                              font_size=dp(12), size_hint_y=None, height=dp(28)))

        # Folder
        fold_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        self._fold_lbl = Label(text="Folder: not set", color=MUTED,
                               font_size=dp(12),
                               text_size=(Window.width*0.65, None), halign="left")
        fold_btn = Button(text="Browse", background_color=CARD, color=ACCENT,
                          size_hint_x=0.3, bold=True)
        fold_btn.bind(on_press=lambda *a: self._pick())
        fold_row.add_widget(self._fold_lbl)
        fold_row.add_widget(fold_btn)
        self.add_widget(fold_row)

        # Settings row
        set_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        set_row.add_widget(Label(text="Format:", color=MUTED,
                                 size_hint_x=0.2, font_size=dp(13)))
        self._fmt = Spinner(text="JPG", values=["JPG","PNG","WEBP"],
                            background_color=CARD, color=FG,
                            size_hint_x=0.3, font_size=dp(13))
        set_row.add_widget(self._fmt)
        set_row.add_widget(Label(text="Quality:", color=MUTED,
                                 size_hint_x=0.2, font_size=dp(13)))
        self._qual = TextInput(text="85", multiline=False,
                               background_color=CARD, foreground_color=FG,
                               size_hint_x=0.3, font_size=dp(14))
        set_row.add_widget(self._qual)
        self.add_widget(set_row)

        # Actions
        act_row = BoxLayout(size_hint_y=None, height=dp(56), spacing=dp(8))
        load_btn = Button(text="📂 Load Images", background_color=BLUE,
                          color=FG, bold=True, size_hint_x=0.4, font_size=dp(14))
        load_btn.bind(on_press=self._load)
        self._conv_btn = Button(text="▶ Convert", background_color=ACCENT,
                                color=BG, bold=True, font_size=dp(15))
        self._conv_btn.bind(on_press=self._start)
        stop_btn = Button(text="⏹", background_color=CARD, color=RED,
                          size_hint_x=0.2, bold=True, font_size=dp(16))
        stop_btn.bind(on_press=lambda *a: setattr(self,"_cancel",True))
        act_row.add_widget(load_btn)
        act_row.add_widget(self._conv_btn)
        act_row.add_widget(stop_btn)
        self.add_widget(act_row)

        self._prog = ProgressBar(max=100, value=0,
                                 size_hint_y=None, height=dp(10))
        self.add_widget(self._prog)
        self._status = Label(text="Ready", color=FG, font_size=dp(13),
                             size_hint_y=None, height=dp(28))
        self.add_widget(self._status)

        sv = ScrollView(size_hint=(1,1))
        self._list_grid = GridLayout(cols=1, spacing=dp(2), size_hint_y=None)
        self._list_grid.bind(minimum_height=self._list_grid.setter("height"))
        sv.add_widget(self._list_grid)
        self.add_widget(sv)

    def _pick(self):
        def cb(path):
            self._folder = path
            self._fold_lbl.text = f"Folder: ...{path[-35:]}"
        FolderPickerPopup(callback=cb).open()

    def _load(self, *a):
        if not self._folder:
            self._status.text = "⚠ Browse a folder first"; return
        p = Path(self._folder)
        self._entries = sorted(
            [f for f in p.rglob("*")
             if f.is_file() and f.suffix.lower() in IMG_EXT
             and "_converted" not in f.stem],
            key=lambda x: str(x).lower())
        if not self._entries:
            self._status.text = "⚠ No images found"; return
        self._list_grid.clear_widgets()
        for f in self._entries:
            try: kb = f.stat().st_size//1024
            except: kb = 0
            sz = f"{kb:,}KB" if kb<1024 else f"{kb/1024:.1f}MB"
            row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(4))
            row.add_widget(Label(text=f.name, color=FG, font_size=dp(11),
                                 halign="left", text_size=(Window.width*0.55, None)))
            row.add_widget(Label(text=sz, color=MUTED, font_size=dp(11),
                                 size_hint_x=0.2))
            stat = Label(text="—", color=MUTED, font_size=dp(11), size_hint_x=0.25)
            row.add_widget(stat)
            row._stat = stat; row._path = f
            self._list_grid.add_widget(row)
        self._status.text = f"Loaded {len(self._entries)} image(s)"

    @mainthread
    def _update_row(self, path, text, color=GREEN):
        for row in self._list_grid.children:
            if hasattr(row,"_path") and row._path==path:
                row._stat.text=text; row._stat.color=color; break

    @mainthread
    def _update(self, status=None, prog=None):
        if status: self._status.text=status
        if prog is not None: self._prog.value=prog

    def _start(self, *a):
        if not PIL_OK:
            self._status.text="⚠ Pillow not installed — pip install Pillow"; return
        if not self._entries:
            self._status.text="⚠ Load images first"; return
        self._cancel=False
        self._conv_btn.disabled=True
        try: qual=int(self._qual.text)
        except: qual=85
        threading.Thread(target=self._run,
                         args=(self._fmt.text, qual), daemon=True).start()

    def _run(self, fmt, qual):
        from PIL import ImageOps
        ext_map = {"JPG":".jpg","PNG":".png","WEBP":".webp"}
        pil_fmt = {"JPG":"JPEG","PNG":"PNG","WEBP":"WEBP"}
        out_ext = ext_map[fmt]; out_fmt = pil_fmt[fmt]
        total = len(self._entries)
        done  = saved = 0

        for i, src in enumerate(self._entries):
            if self._cancel: break
            self._update(status=f"[{i+1}/{total}] {src.name}",
                         prog=int(i/total*100))
            self._update_row(src, "converting…", BLUE)

            out = src.parent / (src.stem + "_converted" + out_ext)
            ct = 2
            while out.exists():
                out = src.parent / (src.stem+f"_converted_{ct}"+out_ext); ct+=1

            try:
                img = Image.open(src)
                # Preserve EXIF
                exif_bytes = None
                try:
                    exif_obj = img.getexif()
                    exif_bytes = exif_obj.tobytes() if exif_obj else None
                except: pass
                if not exif_bytes and "exif" in img.info:
                    exif_bytes = img.info["exif"]

                # Fix orientation
                img = ImageOps.exif_transpose(img)

                # Reset orientation tag
                if exif_bytes:
                    try:
                        import piexif
                        ed = piexif.load(exif_bytes)
                        if "0th" in ed: ed["0th"][piexif.ImageIFD.Orientation]=1
                        exif_bytes = piexif.dump(ed)
                    except: pass

                # Convert mode
                if out_fmt in ("JPEG","WEBP") and img.mode in ("RGBA","P","LA"):
                    bg = Image.new("RGB",img.size,(255,255,255))
                    try: bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA","LA") else None)
                    except: bg.paste(img)
                    img = bg
                elif img.mode not in ("RGB","L","RGBA"):
                    img = img.convert("RGB")

                kw = {}
                if out_fmt=="JPEG":   kw={"quality":qual,"optimize":True}
                elif out_fmt=="WEBP": kw={"quality":qual,"method":4}
                elif out_fmt=="PNG":
                    comp = max(0,min(9,int((100-qual)/10)))
                    kw   = {"compress_level":comp,"optimize":True}
                if exif_bytes: kw["exif"]=exif_bytes
                img.save(out, out_fmt, **kw)

                # Copy timestamps
                try: st=src.stat(); os.utime(str(out),(st.st_atime,st.st_mtime))
                except: pass

                orig_kb = src.stat().st_size//1024
                new_kb  = out.stat().st_size//1024
                pct     = int((1-new_kb/orig_kb)*100) if orig_kb>0 else 0
                saved  += (orig_kb-new_kb)/1024
                self._update_row(src, f"✓ {pct}% saved", GREEN)
            except Exception as ex:
                self._update_row(src, f"error: {str(ex)[:20]}", RED)
            done+=1
            self._update(prog=int((i+1)/total*100))

        self._update(status=("Stopped" if self._cancel
                              else f"✓ Done — {done} converted, ~{saved:.1f}MB freed"),
                     prog=100)
        self._conv_btn.disabled=False


# ══════════════════════════════════════════════════════════════════════
#  MAIN APP
# ══════════════════════════════════════════════════════════════════════

class PhotoSorterApp(App):
    def build(self):
        self.title = "PhotoSorter Pro"

        # Request Android storage permission
        try:
            from android.permissions import request_permissions, Permission
            request_permissions([
                Permission.READ_EXTERNAL_STORAGE,
                Permission.WRITE_EXTERNAL_STORAGE,
                Permission.MANAGE_EXTERNAL_STORAGE,
            ])
        except ImportError:
            pass  # Not on Android (testing on PC)

        root = BoxLayout(orientation="vertical")

        # Title bar
        bar = BoxLayout(size_hint_y=None, height=dp(48),
                        padding=dp(8), spacing=dp(8))
        bar.add_widget(Label(text="[b]PhotoSorter Pro[/b]",
                             markup=True, color=ACCENT,
                             font_size=dp(18), size_hint_x=None, width=dp(200)))
        root.add_widget(bar)

        # Tabs
        tp = TabbedPanel(do_default_tab=False)
        tp.tab_width = Window.width / 3

        for title, widget_cls in [
            ("Sort & Rename",  SortTab),
            ("Video",          VideoCompressorTab),
            ("Images",         ImageConverterTab),
        ]:
            item = TabbedPanelItem(text=title, font_size=dp(13))
            content = widget_cls()
            item.add_widget(content)
            tp.add_widget(item)

        root.add_widget(tp)
        return root


if __name__ == "__main__":
    PhotoSorterApp().run()
