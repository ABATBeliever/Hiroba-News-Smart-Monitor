#!/usr/bin/env python3
"""Hiroba News Smart Monitor v3 - run.py

使い方:  python run.py
設定は、このファイルと同じフォルダの config.toml に書きます(引数はありません)。
標準ライブラリのみ・Python 3.8以上で動きます。
"""
import html
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None

VERSION = "3.0"
BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.toml"

ALL_PAGES = ["home", "weather", "news", "music", "command"]
AUDIO_EXT = {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
MIME_FIX = {".m4a": "audio/mp4", ".opus": "audio/ogg", ".flac": "audio/flac",
            ".aac": "audio/aac", ".avif": "image/avif", ".webp": "image/webp"}


# ======================================================================
# config.toml の読み込み
#   Python 3.11 以上は標準の tomllib を使い、それ未満は下の簡易パーサーを使う
#   (文字列・数値・真偽値・配列・[表]・[[表の配列]] に対応。日時型などは非対応)
# ======================================================================
class _MiniToml:
    BARE = re.compile(r"[A-Za-z0-9_-]+")
    NUM = re.compile(r"[+-]?(?:\d[\d_]*)(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?")

    def __init__(self, text):
        self.t = text
        self.i = 0

    def err(self, msg):
        line = self.t.count("\n", 0, self.i) + 1
        raise ValueError(f"{line}行目: {msg}")

    def eof(self):
        return self.i >= len(self.t)

    def skip(self, newlines=False):
        t = self.t
        while self.i < len(t):
            c = t[self.i]
            if c in " \t" or (newlines and c in "\r\n"):
                self.i += 1
            elif c == "#":
                while self.i < len(t) and t[self.i] != "\n":
                    self.i += 1
            else:
                break

    def expect(self, s):
        if not self.t.startswith(s, self.i):
            self.err(f"'{s}' が必要です")
        self.i += len(s)

    def string(self):
        t, q = self.t, self.t[self.i]
        if t.startswith(q * 3, self.i):
            self.err("複数行文字列には対応していません")
        j = self.i + 1
        if q == "'":  # リテラル文字列(エスケープなし。Windowsのパスに便利)
            k = t.find("'", j)
            if k < 0 or "\n" in t[j:k]:
                self.err("文字列が閉じていません")
            self.i = k + 1
            return t[j:k]
        while j < len(t) and t[j] != '"':
            if t[j] == "\n":
                self.err("文字列が閉じていません")
            j += 2 if t[j] == "\\" else 1
        if j >= len(t):
            self.err("文字列が閉じていません")
        raw = t[self.i:j + 1]
        self.i = j + 1
        try:
            return json.loads(raw)  # TOMLの基本文字列のエスケープはJSONとほぼ同じ
        except ValueError:
            self.err("文字列のエスケープが不正です")

    def key(self):
        self.skip()
        c = self.t[self.i:self.i + 1]
        if c in ('"', "'"):
            return self.string()
        m = self.BARE.match(self.t, self.i)
        if not m:
            self.err("キー名が不正です")
        self.i = m.end()
        return m.group()

    def keypath(self):
        path = [self.key()]
        self.skip()
        while self.t[self.i:self.i + 1] == ".":
            self.i += 1
            path.append(self.key())
            self.skip()
        return path

    def value(self):
        t = self.t
        c = t[self.i:self.i + 1]
        if c in ('"', "'"):
            return self.string()
        if c == "[":
            self.i += 1
            out = []
            while True:
                self.skip(True)
                if t[self.i:self.i + 1] == "]":
                    self.i += 1
                    return out
                out.append(self.value())
                self.skip(True)
                if t[self.i:self.i + 1] == ",":
                    self.i += 1
                elif t[self.i:self.i + 1] != "]":
                    self.err("配列の ',' か ']' が必要です")
        for word, val in (("true", True), ("false", False)):
            if t.startswith(word, self.i):
                self.i += len(word)
                return val
        m = self.NUM.match(t, self.i)
        if not m:
            self.err("値が不正です")
        self.i = m.end()
        s = m.group().replace("_", "")
        return float(s) if re.search(r"[.eE]", s) else int(s)

    @staticmethod
    def _dive(d, k):
        d = d.setdefault(k, {})
        return d[-1] if isinstance(d, list) else d

    def parse(self):
        root, cur = {}, None
        cur = root
        while True:
            self.skip(True)
            if self.eof():
                return root
            if self.t.startswith("[[", self.i):
                self.i += 2
                path = self.keypath()
                self.expect("]]")
                parent = root
                for k in path[:-1]:
                    parent = self._dive(parent, k)
                cur = {}
                parent.setdefault(path[-1], []).append(cur)
            elif self.t[self.i] == "[":
                self.i += 1
                path = self.keypath()
                self.expect("]")
                cur = root
                for k in path:
                    cur = self._dive(cur, k)
            else:
                path = self.keypath()
                self.skip()
                self.expect("=")
                self.skip()
                val = self.value()
                tgt = cur
                for k in path[:-1]:
                    tgt = self._dive(tgt, k)
                tgt[path[-1]] = val
            self.skip()
            if not self.eof() and self.t[self.i] not in "\r\n":
                self.err("1行に複数の設定は書けません")


def load_toml(path):
    raw = path.read_bytes()
    if tomllib:
        return tomllib.loads(raw.decode("utf-8"))
    return _MiniToml(raw.decode("utf-8-sig")).parse()


def warn(msg):
    print(f"[config] {msg}", file=sys.stderr)


def pick(sec, key, default, lo=None, hi=None):
    """型が合わない・範囲外の値は警告して既定値(または範囲内)に直す"""
    if not isinstance(sec, dict) or key not in sec:
        return default
    val = sec[key]
    ok = (isinstance(val, bool) if isinstance(default, bool) else
          isinstance(val, (int, float)) and not isinstance(val, bool) if isinstance(default, float) else
          isinstance(val, int) and not isinstance(val, bool) if isinstance(default, int) else
          isinstance(val, str) if isinstance(default, str) else
          isinstance(val, list))
    if not ok:
        warn(f"{key} の型が不正です。既定値 {default!r} を使います")
        return default
    if isinstance(default, float):
        val = float(val)
    if lo is not None and val < lo:
        warn(f"{key} は {lo} 以上にしてください")
        val = lo
    if hi is not None and val > hi:
        warn(f"{key} は {hi} 以下にしてください")
        val = hi
    return val


def load_config():
    raw = {}
    if CONFIG_PATH.is_file():
        try:
            raw = load_toml(CONFIG_PATH)
        except Exception as e:
            sys.exit(f"config.toml を読み込めません: {e}")
    else:
        warn("config.toml が見つからないため、既定値で起動します")

    def sec(name):
        s = raw.get(name, {})
        return s if isinstance(s, dict) else {}

    srv, dsp, bg, loc, nws, wx, mus, fxs = (sec(n) for n in
                                           ("server", "display", "background", "location", "news", "weather", "music", "fx"))
    fx_pairs = []
    for b in pick(fxs, "pairs", ["usd", "eur", "cny"]):
        if isinstance(b, str) and re.fullmatch(r"[A-Za-z]{3}", b) and b.lower() not in fx_pairs:
            fx_pairs.append(b.lower())

    pages = []
    for p in pick(dsp, "pages", ALL_PAGES):
        p = "command" if p == "commands" else p
        if p in ALL_PAGES and p not in pages:
            pages.append(p)
        elif p not in ALL_PAGES:
            warn(f"pages: 不明なページ名 {p!r} は無視します")
    if not pages:
        warn("pages が空のため home だけ表示します")
        pages = ["home"]

    commands = []
    for c in raw.get("commands", []) if isinstance(raw.get("commands"), list) else []:
        cmd = c.get("cmd") if isinstance(c, dict) else None
        if isinstance(cmd, str) and cmd.strip() or (isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd)):
            commands.append({"name": str(c.get("name") or "無名"), "cmd": cmd, "confirm": bool(c.get("confirm", False))})
        else:
            warn(f"[[commands]] {c.get('name', '?') if isinstance(c, dict) else '?'}: cmd がありません")

    feeds = [u for u in pick(nws, "feeds", ["https://www3.nhk.or.jp/rss/news/cat0.xml"]) if isinstance(u, str) and u.strip()]

    return {
        "host": pick(srv, "host", "localhost"),
        "port": pick(srv, "port", 8765, 1, 65535),
        "pages": pages,
        "show_seconds": pick(dsp, "show_seconds", True),
        "idle_return_sec": pick(dsp, "idle_return_sec", 120, 0),
        "hide_cursor": pick(dsp, "hide_cursor", False),
        "wake_lock": pick(dsp, "wake_lock", True),
        "fullscreen_on_tap": pick(dsp, "fullscreen_on_tap", True),
        "bg_dir": pick(bg, "dir", "images"),
        "bg_interval_sec": pick(bg, "interval_sec", 30, 0),
        "bg_dim": pick(bg, "dim", 0.55, 0.0, 0.95),
        "city": pick(loc, "name", "東京"),
        "lat": pick(loc, "lat", 35.6895, -90.0, 90.0),
        "lon": pick(loc, "lon", 139.6917, -180.0, 180.0),
        "tz": pick(loc, "timezone", "Asia/Tokyo"),
        "feeds": feeds,
        "news_count": pick(nws, "count", 20, 1, 60),
        "home_news_count": pick(nws, "home_count", 6, 1, 20),
        "news_min": pick(nws, "refresh_min", 10, 1),
        "weather_min": pick(wx, "refresh_min", 30, 1),
        "fx_pairs": fx_pairs,
        "fx_min": pick(fxs, "refresh_min", 60, 1),
        "music_dir": pick(mus, "dir", "music"),
        "music_volume": pick(mus, "volume", 0.8, 0.0, 1.0),
        "music_mode": pick(mus, "mode", "seq"),
        "commands": commands,
    }


def resolve_dir(p):
    p = Path(p)
    return p if p.is_absolute() else BASE / p


CFG = load_config()
BG_DIR = resolve_dir(CFG["bg_dir"]) if CFG["bg_dir"] else None
MUSIC_DIR = resolve_dir(CFG["music_dir"]) if CFG["music_dir"] else None


# ======================================================================
# データ取得(要求があったときだけ取得し、キャッシュする。常駐スレッドはなし)
# ======================================================================
def http_get(url, timeout=8, limit=3_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (HirobaNewsSmartMonitor)",
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit)


def _r(x):
    return None if x is None else round(x)


def fetch_weather():
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={CFG['lat']}&longitude={CFG['lon']}"
           "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
           "&hourly=temperature_2m,precipitation_probability,weather_code"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "precipitation_probability_max,sunrise,sunset"
           f"&wind_speed_unit=ms&timezone={urllib.parse.quote(CFG['tz'], safe='')}&forecast_days=7")
    w = json.loads(http_get(url))
    c, h, d = w["current"], w["hourly"], w["daily"]
    times = h["time"]
    now_hour = c["time"][:13]
    start = next((i for i, t in enumerate(times) if t[:13] >= now_hour), 0)
    return {
        "cur": {"t": _r(c["temperature_2m"]), "feel": _r(c["apparent_temperature"]),
                "hum": c["relative_humidity_2m"], "wind": round(c["wind_speed_10m"], 1),
                "code": c["weather_code"]},
        "hourly": [{"h": int(times[i][11:13]), "t": _r(h["temperature_2m"][i]),
                    "pop": h["precipitation_probability"][i], "code": h["weather_code"][i]}
                   for i in range(start, min(start + 24, len(times)), 2)],
        "daily": [{"d": d["time"][i], "code": d["weather_code"][i],
                   "min": _r(d["temperature_2m_min"][i]), "max": _r(d["temperature_2m_max"][i]),
                   "pop": d["precipitation_probability_max"][i]} for i in range(len(d["time"]))],
        "sun": {"rise": d["sunrise"][0][11:16], "set": d["sunset"][0][11:16]},
    }


def _ln(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _clean(s, n=120):
    s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", s or ""))).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def fetch_feed(url):
    """RSS 2.0 / RSS 1.0(RDF) / Atom に対応"""
    root = ET.fromstring(http_get(url, timeout=8, limit=2_000_000))
    src = ""
    for parent in [root] + [c for c in root if _ln(c.tag) == "channel"]:
        src = next(((c.text or "").strip() for c in parent if _ln(c.tag) == "title" and (c.text or "").strip()), "")
        if src:
            break
    items = []
    for e in root.iter():
        if _ln(e.tag) not in ("item", "entry"):
            continue
        f = {}
        for c in e:
            k = _ln(c.tag)
            if k == "link":
                f.setdefault("link", c.get("href") or (c.text or "").strip())
            elif k in ("title", "description", "summary"):
                f.setdefault(k, c.text or "")
        title = _clean(f.get("title"), 200)
        if not title:
            continue
        desc = _clean(f.get("description") or f.get("summary"))
        items.append({"s": _clean(src, 30), "t": title, "d": "" if desc == title else desc, "u": f.get("link", "")})
    return items


def fetch_news():
    n = CFG["news_count"]
    feeds = CFG["feeds"]
    if not feeds:
        raise RuntimeError("config.toml の [news] feeds が空です")
    last = [None]

    def one(u):
        try:
            return fetch_feed(u)[:n]
        except Exception as e:
            last[0] = f"{urllib.parse.urlsplit(u).netloc}: {type(e).__name__}: {e}"
            return []

    with ThreadPoolExecutor(max_workers=min(len(feeds), 6)) as ex:
        lists = list(ex.map(one, feeds))
    if not any(lists):
        raise RuntimeError(last[0] or "記事がありません")
    out, seen, i = [], set(), 0  # フィードを交互に並べる(1社に偏らないように)
    while len(out) < n and any(i < len(x) for x in lists):
        for x in lists:
            if i < len(x) and len(out) < n and x[i]["t"] not in seen:
                seen.add(x[i]["t"])
                out.append(x[i])
        i += 1
    return out


def _fx_rate(date, base):
    """currency-api(ECBベースの日次レート)。CDNが落ちていたらミラーを試す"""
    for url in (f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@{date}/v1/currencies/{base}.min.json",
                f"https://{date}.currency-api.pages.dev/v1/currencies/{base}.min.json"):
        try:
            return float(json.loads(http_get(url, timeout=8, limit=1_000_000))[base]["jpy"])
        except Exception:
            pass
    return None


def fetch_fx():
    week = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
    bases = CFG["fx_pairs"]
    jobs = [(d, b) for b in bases for d in ("latest", week)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(lambda j: _fx_rate(*j), jobs))
    out = [{"pair": b.upper() + "/JPY", "rate": res[2 * i], "prev": res[2 * i + 1]}
           for i, b in enumerate(bases) if res[2 * i] is not None]
    if not out:
        raise RuntimeError("為替を取得できません")
    return out


class Source:
    """取得結果のキャッシュ。失敗しても前回の値を保持し、60秒後まで再取得しない"""

    def __init__(self, fn, ttl_min):
        self.fn, self.ttl = fn, ttl_min * 60
        self.lock = threading.Lock()
        self.data = self.ts = self.err = None
        self.next = 0.0

    def get(self):
        with self.lock:
            now = time.time()
            if now >= self.next:
                try:
                    self.data, self.ts, self.err = self.fn(), now, None
                    self.next = now + self.ttl
                except Exception as e:
                    print(f"[warn] {self.fn.__name__}: {e}", file=sys.stderr)
                    self.err = f"{type(e).__name__}: {e}"[:160]
                    self.next = now + min(60, self.ttl)
            return self.data, self.ts, self.err


SOURCES = {"weather": Source(fetch_weather, CFG["weather_min"]),
           "news": Source(fetch_news, CFG["news_min"])}
if CFG["fx_pairs"]:
    SOURCES["fx"] = Source(fetch_fx, CFG["fx_min"])


def get_data(keys):
    out, ts, err = {}, {}, {}
    for k in keys:
        if k in SOURCES:
            data, t, e = SOURCES[k].get()
            if data is not None:
                out[k], ts[k] = data, t
            if e:
                err[k] = e
    out["_ts"], out["_err"] = ts, err
    return out


# ---- 音楽・画像 ----
def _natkey(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.casefold())]


def scan_music(d, rel="", depth=0):
    dirs, files = [], []
    if depth > 8:
        return []
    try:
        entries = sorted(os.scandir(d), key=lambda e: _natkey(e.name))
    except OSError:
        return []
    for e in entries:
        if e.name.startswith("."):
            continue
        if e.is_dir():
            c = scan_music(e.path, rel + e.name + "/", depth + 1)
            if c:
                dirs.append({"n": e.name, "c": c})
        elif os.path.splitext(e.name)[1].lower() in AUDIO_EXT:
            files.append({"n": e.name, "p": rel + e.name})
    return dirs + files


def list_images():
    if not BG_DIR or not BG_DIR.is_dir():
        return []
    return sorted(p.name for p in BG_DIR.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXT and not p.name.startswith("."))


def safe_join(root, rel):
    """root の外に出るパスを拒否する"""
    root = os.path.realpath(root)
    fp = os.path.realpath(os.path.join(root, rel))
    return fp if fp.startswith(root + os.sep) and os.path.isfile(fp) else None


def run_command(i):
    c = CFG["commands"][i]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(BASE))
    if os.name == "posix":
        kw["start_new_session"] = True
    if isinstance(c["cmd"], list):
        subprocess.Popen(c["cmd"], **kw)          # 配列: シェルを通さず直接実行
    else:
        subprocess.Popen(c["cmd"], shell=True, **kw)  # 文字列: シェル経由


# ======================================================================
# HTTP
# ======================================================================
def client_config():
    c = CFG
    return {
        "pages": c["pages"], "showSeconds": c["show_seconds"], "idleReturn": c["idle_return_sec"], "hideCursor": c["hide_cursor"],
        "wakeLock": c["wake_lock"], "fullscreenOnTap": c["fullscreen_on_tap"],
        "bgInterval": c["bg_interval_sec"], "bgDim": c["bg_dim"], "bgEnabled": bool(c["bg_dir"]),
        "fx": bool(c["fx_pairs"]), "city": c["city"], "homeNews": c["home_news_count"],
        "newsMin": c["news_min"], "weatherMin": c["weather_min"],
        "volume": c["music_volume"], "mode": c["music_mode"],
        "commands": [{"name": x["name"], "confirm": x["confirm"]} for x in c["commands"]],  # cmd本体は送らない
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype, status=200, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                   "application/json; charset=utf-8", status, {"Cache-Control": "no-store"})

    def _file(self, fp, cache):
        """Range対応・チャンク送信(曲を丸ごとメモリに載せない)"""
        size = os.path.getsize(fp)
        ctype = MIME_FIX.get(os.path.splitext(fp)[1].lower()) or mimetypes.guess_type(fp)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            a, b = m.groups()
            if not a:
                start = max(0, size - int(b))
            else:
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", cache)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        left = end - start + 1
        with open(fp, "rb") as f:
            f.seek(start)
            while left > 0:
                buf = f.read(min(65536, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)

    def do_GET(self):
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 再生の途中移動などで接続が切れただけ

    def _get(self):
        u = urllib.parse.urlsplit(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            page = (BASE / "index.html").read_text(encoding="utf-8")
            inject = "<script>window.CFG=" + json.dumps(client_config(), ensure_ascii=False).replace("</", "<\\/") + ";</script>"
            self._send(page.replace("</head>", inject + "</head>", 1).encode("utf-8"), "text/html; charset=utf-8",
                       headers={"Cache-Control": "no-store"})
        elif p == "/api/data":
            keys = (urllib.parse.parse_qs(u.query).get("k", [""])[0]).split(",")
            self._json(get_data([k for k in keys if k]))
        elif p == "/api/images":
            self._json(list_images())
        elif p == "/api/music":
            self._json(scan_music(MUSIC_DIR) if MUSIC_DIR and MUSIC_DIR.is_dir() else [])
        elif p.startswith("/image/") and BG_DIR:
            name = urllib.parse.unquote(p[len("/image/"):])
            fp = safe_join(BG_DIR, name) if os.path.splitext(name)[1].lower() in IMAGE_EXT else None
            self._file(fp, "public, max-age=3600") if fp else self._send(b"not found", "text/plain", 404)
        elif p.startswith("/music/") and MUSIC_DIR:
            name = urllib.parse.unquote(p[len("/music/"):])
            fp = safe_join(MUSIC_DIR, name) if os.path.splitext(name)[1].lower() in AUDIO_EXT else None
            self._file(fp, "no-cache") if fp else self._send(b"not found", "text/plain", 404)
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        try:
            if self.path.split("?")[0] != "/api/run":
                return self._send(b"not found", "text/plain", 404)
            # 他サイトのページからの勝手な実行(CSRF / DNSリバインディング)を防ぐ
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlsplit(origin).netloc != host:
                return self._json({"ok": False, "error": "forbidden"}, 403)
            if CFG["host"] in ("127.0.0.1", "localhost", "::1") and \
                    urllib.parse.urlsplit("//" + host).hostname not in ("127.0.0.1", "localhost", "::1"):
                return self._json({"ok": False, "error": "forbidden"}, 403)
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._json({"ok": False, "error": "bad content-type"}, 415)
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1024:
                return self._json({"ok": False, "error": "too large"}, 413)
            i = json.loads(self.rfile.read(n) or b"{}").get("i")
            if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(CFG["commands"]):
                return self._json({"ok": False, "error": "unknown command"}, 400)
            run_command(i)  # 実行するのは config.toml に書かれたコマンドだけ(番号で指定)
            self._json({"ok": True})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({"ok": False, "error": str(e)[:120]}, 500)


def main():
    host, port = CFG["host"], CFG["port"]
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        sys.exit(f"{host}:{port} で待ち受けできません: {e}")
    print(f"Hiroba News Smart Monitor v{VERSION}\nhttp://{'localhost' if host in ('0.0.0.0', '::') else host}:{port}")
    print("表示するページ: " + " / ".join(CFG["pages"]))
    if host not in ("127.0.0.1", "localhost", "::1") and CFG["commands"]:
        print("注意: 同じネットワークのどの端末からでも、コマンドページの操作ができる状態です")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped")


if __name__ == "__main__":
    main()
