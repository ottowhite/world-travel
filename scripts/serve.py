"""Dynamic viewer for CHELSA monthly climatologies.

A tiny HTTP server that renders the visible map region on demand by reading
windowed crops straight from the local GeoTIFFs (which carry internal overviews,
so reads are fast at every zoom). The browser front-end is a full-page canvas
that pans (right-drag), zooms (wheel), and wraps around on both axes; each
pan/zoom asks the server for a fresh render at the current resolution, so detail
scales with zoom level.

Run:
    make serve                      # -> http://localhost:8765
    uv run scripts/serve.py --port 9000

Endpoints:
    GET /                           the viewer page
    GET /render?var=&month=&west=&east=&south=&north=&w=&h=&mask=
                                    PNG image (RGBA, w x h) normalised against the
                                    variable's fixed colour range. mask=1 paints ocean
                                    (outside Natural Earth land) pale blue. PNG cuts
                                    payloads ~10x vs raw RGBA over the wire — important
                                    for remote viewers, free for loopback.
    GET /coastline.geojson          the bundled Natural Earth coastline overlay
"""

from __future__ import annotations

import argparse
import io
import json
import math
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import rasterio
from cmcrameri import cm as cmc
from PIL import Image
from rasterio.features import rasterize
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.windows import Window, from_bounds

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
ASSETS_DIR = ROOT / "assets"
COASTLINE_PATH = ASSETS_DIR / "ne_50m_coastline.geojson"
LAND_PATH = ASSETS_DIR / "ne_50m_land.geojson"
PERIOD = "1981-2010"
VERSION = "V.2.1"
SCALE = 0.1  # CHELSA V2.1 DN -> physical, before any offset
MAX_PX = 2200  # cap render size to bound work
MAX_COPIES = 12  # cap wrap tiles per axis
OCEAN_RGB = (198, 221, 240)  # pale blue painted over ocean when masking is on

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# A variable's colour map is either a named scientific colormap ("cmap") sampled
# directly into the LUT, or a list of (position, RGB) anchor "stops" interpolated
# into the LUT. The client builds its colour-bar gradient from "stops", so cmap
# variables also expose a sampled "stops" list (see _cmap_stops).
VARIABLES = {
    "tas": {
        "label": "Temperature", "unit": "°C",
        "convert": lambda k: k - 273.15,  # Kelvin (DN*0.1) -> °C
        "vmin": -40.0, "vmax": 40.0,      # fixed absolute display range (°C)
        # Full ROYGBIV spectrum: violet = coldest, red = hottest (note: a rainbow
        # ramp is not perceptually uniform — chosen here for look, by request).
        "stops": [(0.0, (148, 0, 211)),       # violet  (cold)
                  (1 / 6, (75, 0, 130)),       # indigo
                  (2 / 6, (0, 0, 255)),        # blue
                  (3 / 6, (0, 180, 0)),        # green
                  (4 / 6, (255, 255, 0)),      # yellow
                  (5 / 6, (255, 127, 0)),      # orange
                  (1.0, (255, 0, 0))],         # red     (hot)
    },
    "pr": {
        "label": "Precipitation", "unit": "mm / month",
        "convert": lambda mm: mm,
        # Precip is strongly right-skewed (deserts ~0, monsoons >1000 mm/month),
        # so a LINEAR map paints most land pale. Use a LOG10 colour scale over a
        # fixed range [vmin, vmax] mm/month — values clamp to vmin (palest) below
        # and to vmax (deepest) above. Saturate at 400 mm (already very heavy) to
        # spend the whole ramp on the common range and maximise visible detail.
        "vmin": 1.0, "vmax": 400.0,       # fixed absolute display range (mm/month)
        "log": True,
        # devon_r: perceptually-uniform sequential map (Fabio Crameri), reversed so
        # dry = pale, wet = deep saturated blue (ends deep blue, not pure black).
        "cmap": "devon_r",
    },
}


def tif_path(var: str, month: int) -> Path:
    return DATA_DIR / f"CHELSA_{var}_{month:02d}_{PERIOD}_{VERSION}.tif"


@lru_cache(maxsize=None)
def _lut(var: str) -> np.ndarray:
    cfg = VARIABLES[var]
    if "cmap" in cfg:
        # Sample a named scientific colormap (e.g. vik) at 256 points -> uint8 RGB.
        cmap = getattr(cmc, cfg["cmap"])
        rgba = cmap(np.linspace(0, 1, 256))          # (256, 4) floats 0..1
        return (rgba[:, :3] * 255).round().astype(np.uint8)
    stops = cfg["stops"]
    xs = [s[0] for s in stops]
    grid = np.linspace(0, 1, 256)
    lut = np.zeros((256, 3), np.uint8)
    for ch in range(3):
        lut[:, ch] = np.interp(grid, xs, [s[1][ch] for s in stops]).astype(np.uint8)
    return lut


# Palette layout for the indexed PNG output of /render:
#   0                       : nodata (made transparent via PNG tRNS, alpha=0)
#   1..PAL_DATA_LEVELS      : data colours, resampled from the 256-entry _lut
#   255                     : ocean fill (used only when mask=1)
# 64 data levels is the sweet spot for wire size: 1.25 °C per band on tas
# (-40..40 °C) and ~12 levels per decade on the pr log scale — visually
# indistinguishable from the full 256-entry LUT at any reasonable zoom, while
# halving the PNG vs 254 levels because deflate sees ~4x fewer distinct bytes
# in the pixel stream.
PAL_NODATA = 0
PAL_OCEAN = 255
PAL_DATA_LEVELS = 64


@lru_cache(maxsize=None)
def _palette_bytes(var: str) -> bytes:
    """Flat 768-byte RGB palette for PIL.Image.putpalette()."""
    lut = _lut(var)                                       # (256, 3) uint8
    src = np.round(np.linspace(0, 255, PAL_DATA_LEVELS)).astype(int)
    pal = np.zeros((256, 3), np.uint8)
    pal[1:1 + PAL_DATA_LEVELS] = lut[src]
    pal[PAL_OCEAN] = OCEAN_RGB
    return pal.tobytes()


def _client_stops(var: str):
    """RGB anchor stops for the client colour-bar gradient (positions 0..1)."""
    cfg = VARIABLES[var]
    if "cmap" not in cfg:
        return cfg["stops"]
    cmap = getattr(cmc, cfg["cmap"])
    pos = np.linspace(0, 1, 33)
    rgba = cmap(pos)
    return [[round(float(p), 4), [int(round(c * 255)) for c in rgba[i, :3]]]
            for i, p in enumerate(pos)]


@lru_cache(maxsize=1)
def _coastline_bytes() -> bytes:
    """Read the bundled Natural Earth coastline GeoJSON once and cache it."""
    return COASTLINE_PATH.read_bytes()


@lru_cache(maxsize=1)
def _land_geoms():
    """Natural Earth land polygons (GeoJSON geometries) for the ocean mask."""
    gj = json.loads(LAND_PATH.read_text())
    return [f["geometry"] for f in gj["features"] if f.get("geometry")]


@lru_cache(maxsize=1)
def _bounds():
    # All variables/months share the same grid; read it once.
    with rasterio.open(tif_path("tas", 1)) as ds:
        b = ds.bounds
    return b.left, b.bottom, b.right, b.top


def render(var, month, west, east, south, north, w, h, mask=False):
    """Assemble the view bbox into a (h, w) float array, wrapping on both axes.

    When `mask` is set, pixels outside the Natural Earth land polygons are painted
    a pale ocean blue instead of showing the (ocean-covered) source data.
    """
    cfg = VARIABLES[var]
    left, bottom, right, top = _bounds()
    world_w, world_h = right - left, top - bottom
    out = np.full((h, w), np.nan, dtype="float32")
    land = np.zeros((h, w), bool) if mask else None
    dppx, dppy = (east - west) / w, (north - south) / h

    # Integer "world copies" the view overlaps, clamped so we never tile forever.
    kx0 = math.floor((west - left) / world_w)
    kx1 = math.floor((east - left) / world_w)
    ky0 = math.floor((top - north) / world_h)
    ky1 = math.floor((top - south) / world_h)
    kx1 = min(kx1, kx0 + MAX_COPIES)
    ky1 = min(ky1, ky0 + MAX_COPIES)

    with rasterio.open(tif_path(var, month + 1)) as ds:
        for kx in range(kx0, kx1 + 1):
            cw, ce = left + kx * world_w, right + kx * world_w
            for ky in range(ky0, ky1 + 1):
                ct, cb = top - ky * world_h, bottom - ky * world_h
                ow, oe = max(west, cw), min(east, ce)        # overlap in view deg
                on, os = min(north, ct), max(south, cb)
                if oe <= ow or on <= os:
                    continue
                # destination pixels in the output image
                cx0, cx1 = round((ow - west) / dppx), round((oe - west) / dppx)
                ry0, ry1 = round((north - on) / dppy), round((north - os) / dppy)
                if cx1 <= cx0 or ry1 <= ry0:
                    continue
                # same patch in original (un-shifted) source coordinates
                win = from_bounds(ow - kx * world_w, os + ky * world_h,
                                  oe - kx * world_w, on + ky * world_h, ds.transform)
                arr = ds.read(1, window=win, out_shape=(ry1 - ry0, cx1 - cx0),
                              resampling=rasterio.enums.Resampling.average,
                              boundless=True, masked=True).astype("float32")
                patch = cfg["convert"](np.ma.filled(arr, np.nan) * SCALE)
                out[ry0:ry1, cx0:cx1] = patch
                if mask:
                    tr = transform_from_bounds(
                        ow - kx * world_w, os + ky * world_h,
                        oe - kx * world_w, on + ky * world_h,
                        cx1 - cx0, ry1 - ry0)
                    lm = rasterize(_land_geoms(), out_shape=(ry1 - ry0, cx1 - cx0),
                                   transform=tr, fill=0, default_value=1,
                                   dtype="uint8")
                    land[ry0:ry1, cx0:cx1] = lm.astype(bool)

    # Fixed absolute colour range from the variable's config — independent of the
    # region in view, so the same colour always means the same physical value.
    vmin, vmax = cfg["vmin"], cfg["vmax"]

    finite = np.isfinite(out)
    vals = out[finite]
    if cfg.get("log"):
        # Normalise in log10 space: clamp to [vmin, vmax], then map to 0..1.
        vc = np.clip(vals, vmin, vmax)
        norm = (np.log10(vc) - math.log10(vmin)) / (math.log10(vmax) - math.log10(vmin))
    else:
        norm = np.clip((vals - vmin) / (vmax - vmin), 0, 1)
    # Data goes into palette slots 1..PAL_DATA_LEVELS; slot 0 stays nodata.
    idx = np.zeros((h, w), np.uint8)
    idx[finite] = (norm * (PAL_DATA_LEVELS - 1)).astype(np.uint8) + 1
    if land is not None:
        idx[~land] = PAL_OCEAN
    return idx


def value_at(var, month, lon, lat):
    """Physical value at a single lon/lat (wraps in longitude), or None if no data."""
    cfg = VARIABLES[var]
    left, bottom, right, top = _bounds()
    lon = ((lon - left) % (right - left)) + left   # wrap into [left, right)
    if not (bottom <= lat <= top):
        return None
    with rasterio.open(tif_path(var, month + 1)) as ds:
        row, col = ds.index(lon, lat)
        if not (0 <= row < ds.height and 0 <= col < ds.width):
            return None
        v = ds.read(1, window=Window(col, row, 1, 1), masked=True)
    if v.size == 0 or np.ma.is_masked(v) and v.mask.all():
        return None
    return float(cfg["convert"](float(v[0, 0]) * SCALE))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # keep the console quiet
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send_html()
        if url.path == "/render":
            return self._send_render(parse_qs(url.query))
        if url.path == "/value":
            return self._send_value(parse_qs(url.query))
        if url.path == "/coastline.geojson":
            return self._send_coastline()
        self.send_error(404)

    def _send_value(self, q):
        try:
            var = q["var"][0]
            month = int(q["month"][0])
            lon, lat = float(q["lon"][0]), float(q["lat"][0])
            assert var in VARIABLES and 0 <= month < 12
        except (KeyError, ValueError, AssertionError):
            return self.send_error(400)
        val = value_at(var, month, lon, lat)
        body = json.dumps({"value": val, "unit": VARIABLES[var]["unit"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_coastline(self):
        try:
            body = _coastline_bytes()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_render(self, q):
        try:
            var = q["var"][0]
            month = int(q["month"][0])
            west, east = float(q["west"][0]), float(q["east"][0])
            south, north = float(q["south"][0]), float(q["north"][0])
            w = min(int(q["w"][0]), MAX_PX)
            h = min(int(q["h"][0]), MAX_PX)
            do_mask = q.get("mask", ["0"])[0] == "1"
            assert var in VARIABLES and 0 <= month < 12 and w > 0 and h > 0
        except (KeyError, ValueError, AssertionError):
            return self.send_error(400)

        idx = render(var, month, west, east, south, north, w, h, mask=do_mask)
        # Palette PNG: 1 byte per pixel + a 768-byte palette, vs the 4 bytes/pixel
        # we'd ship as RGBA. tRNS marks palette index 0 (PAL_NODATA) transparent,
        # so NaN areas (and unmasked oceans) render alpha=0 in the browser. With
        # CHELSA's banded colours this typically lands around 4x smaller than the
        # RGBA PNG while staying visually identical.
        img = Image.fromarray(idx, mode="P")
        img.putpalette(_palette_bytes(var))
        buf = io.BytesIO()
        img.save(buf, format="PNG", compress_level=9, transparency=PAL_NODATA)
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _config_json():
    return json.dumps({
        "months": MONTHS,
        "order": list(VARIABLES),
        "vars": {k: {"label": v["label"], "unit": v["unit"],
                     "vmin": v["vmin"], "vmax": v["vmax"],
                     "log": bool(v.get("log")),
                     "stops": _client_stops(k)} for k, v in VARIABLES.items()},
        "bounds": _bounds(),  # left, bottom, right, top
    })


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CHELSA climatology viewer</title>
<style>
  html, body { margin: 0; height: 100%; overflow: hidden;
               font-family: system-ui, sans-serif; background: #11151c; }
  #map { position: fixed; inset: 0; width: 100vw; height: 100vh; display: block;
         cursor: grab; }
  #map.panning { cursor: grabbing; }
  #bar { position: fixed; top: 10px; left: 10px; display: flex; gap: 12px;
         align-items: center; padding: 7px 12px; border-radius: 10px;
         background: rgba(20,24,32,.82); color: #eee; backdrop-filter: blur(4px);
         font-size: 14px; z-index: 10; }
  #bar .g { display: flex; align-items: center; gap: 6px; }
  #bar button { padding: 4px 11px; border: 1px solid #4a5568; border-radius: 6px;
                background: #2d3748; color: #eee; cursor: pointer; font-size: 14px; }
  #bar button.active { background: #3182ce; border-color: #3182ce; }
  #monthBox { display: flex; flex-direction: column; gap: 1px; }
  #month { width: 432px; margin: 0; }
  #monthTicks { position: relative; width: 432px; height: 13px; }
  #monthTicks span { position: absolute; transform: translateX(-50%);
                     font-size: 10px; color: #aab4c0; cursor: pointer; }
  #monthTicks span.active { color: #fff; font-weight: 700; }
  #title { position: fixed; top: 10px; right: 14px; color: #ddd; z-index: 10;
           font-size: 14px; text-shadow: 0 1px 2px #000; }
  #cbar { position: fixed; right: 16px; bottom: 18px; width: 22px; height: 200px;
          border: 1px solid #0008; border-radius: 4px; z-index: 10; }
  #cbarWrap { position: fixed; right: 44px; bottom: 18px; height: 200px; z-index: 10;
              display: flex; flex-direction: column; justify-content: space-between;
              color: #eee; font-size: 12px; text-shadow: 0 1px 2px #000; text-align: right; }
  #hint { position: fixed; left: 12px; bottom: 12px; color: #9aa5b1; font-size: 12px;
          z-index: 10; }
  #tip { position: fixed; pointer-events: none; z-index: 20; padding: 3px 7px;
         border-radius: 5px; background: rgba(20,24,32,.9); color: #fff;
         font-size: 12px; white-space: nowrap; display: none; }
</style>
</head>
<body>
<canvas id="map"></canvas>
<div id="bar">
  <div class="g" id="vars"><strong>Variable:</strong></div>
  <div class="g">
    <button id="play">▶</button>
    <div id="monthBox">
      <input id="month" type="range" min="0" max="11" step="1" value="0" list="monthticks"/>
      <datalist id="monthticks"></datalist>
      <div id="monthTicks"></div>
    </div>
  </div>
  <div class="g">
    <label><input id="coastToggle" type="checkbox" checked/> Coastlines</label>
  </div>
</div>
<div id="title"></div>
<div id="cbarWrap"><span id="vmax">–</span><span id="vmid"></span><span id="vmin">–</span></div>
<canvas id="cbar"></canvas>
<div id="hint">drag to pan · scroll to zoom · hover to read values · wraps around</div>
<div id="tip"></div>
<script>
const CFG = __CONFIG__;
const [L, B, R, T] = CFG.bounds;        // source extent (deg)
const WORLD_W = R - L, WORLD_H = T - B;
const canvas = document.getElementById('map');
const ctx = canvas.getContext('2d');

let curVar = CFG.order[0], curMonth = 0;
const view = { cx: 0, cy: (T + B) / 2, dpp: 1 };   // centre lon/lat, deg per backing-px
let last = null;                                    // {bitmap, west, east, south, north}
let pending = null, refreshTimer = null, playing = false;
let coastlines = null;                              // [[ [lon,lat], ... ], ...]
let showCoast = true;
// LRU cache of decoded render bitmaps keyed by exact request URL. A full year of
// play at one bbox+variable is 12 entries, and toggling the variable doubles
// that, so 24 covers the common re-watch case without growing unbounded under
// panning (each pan changes the URL and eventually evicts old entries).
const renderCache = new Map();
const RENDER_CACHE_LIMIT = 24;

// Fetch + flatten the bundled Natural Earth coastline once. LineString and
// MultiLineString geometries both collapse to a flat list of [lon,lat] polylines.
async function loadCoastlines() {
  try {
    const r = await fetch('/coastline.geojson');
    const gj = await r.json();
    const out = [];
    for (const f of gj.features) {
      const g = f.geometry; if (!g) continue;
      if (g.type === 'LineString') out.push(g.coordinates);
      else if (g.type === 'MultiLineString') for (const c of g.coordinates) out.push(c);
    }
    coastlines = out;
    draw();
  } catch (e) { console.error('coastline load failed', e); }
}

function sizeCanvas() {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(canvas.clientWidth * dpr);
  canvas.height = Math.round(canvas.clientHeight * dpr);
}
function clampDpp(d) {
  const maxD = 3 * WORLD_W / canvas.width;          // zoom out: ~3 world copies wide
  return Math.max(0.0004, Math.min(d, maxD));       // zoom in: oversample 30 arc-sec
}
function bbox() {
  const hw = canvas.width / 2 * view.dpp, hh = canvas.height / 2 * view.dpp;
  return { west: view.cx - hw, east: view.cx + hw,
           south: view.cy - hh, north: view.cy + hh };
}

function draw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!last) return;
  const v = bbox();
  const dppx = (v.east - v.west) / canvas.width, dppy = (v.north - v.south) / canvas.height;
  for (let kx = -MAXK; kx <= MAXK; kx++) {
    for (let ky = -MAXK; ky <= MAXK; ky++) {
      const gw = last.west + kx * WORLD_W, ge = last.east + kx * WORLD_W;
      const gn = last.north + ky * WORLD_H, gs = last.south + ky * WORLD_H;
      const dx = (gw - v.west) / dppx, dw = (ge - gw) / dppx;
      const dy = (v.north - gn) / dppy, dh = (gn - gs) / dppy;
      if (dx > canvas.width || dx + dw < 0 || dy > canvas.height || dy + dh < 0) continue;
      ctx.drawImage(last.bitmap, dx, dy, dw, dh);
    }
  }
  if (showCoast && coastlines) drawCoastlines(v, dppx, dppy);
}
const MAXK = 4;

function drawCoastlines(v, dppx, dppy) {
  ctx.lineJoin = 'round'; ctx.lineCap = 'round';
  // Draw each path twice across the same wrap copies the raster uses: a wider
  // dark halo then a thin light line, so it reads over both vik (blue/red) and
  // devon_r (pale->deep-blue) backgrounds.
  for (let kx = -MAXK; kx <= MAXK; kx++) {
    // Skip whole copies that fall outside the view in longitude.
    const offx = kx * WORLD_W;
    if (L + offx > v.east || R + offx < v.west) continue;
    for (let ky = -MAXK; ky <= MAXK; ky++) {
      const offy = ky * WORLD_H;
      if (B + offy > v.north || T + offy < v.south) continue;
      ctx.beginPath();
      for (const line of coastlines) {
        for (let i = 0; i < line.length; i++) {
          const x = (line[i][0] + offx - v.west) / dppx;
          const y = (v.north - (line[i][1] + offy)) / dppy;
          if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
      }
      ctx.strokeStyle = 'rgba(0,0,0,0.45)'; ctx.lineWidth = 2.4; ctx.stroke();
      ctx.strokeStyle = 'rgba(255,255,255,0.92)'; ctx.lineWidth = 1; ctx.stroke();
    }
  }
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 110);
}

async function refresh() {
  const v = bbox();
  const w = canvas.width, h = canvas.height;
  const url = `/render?var=${curVar}&month=${curMonth}` +
    `&west=${v.west}&east=${v.east}&south=${v.south}&north=${v.north}&w=${w}&h=${h}` +
    `&mask=${showCoast ? 1 : 0}`;
  const hit = renderCache.get(url);
  if (hit) {
    // Re-insert so this entry is most-recently-used in the Map's iteration order.
    renderCache.delete(url); renderCache.set(url, hit);
    last = { bitmap: hit, west: v.west, east: v.east, south: v.south, north: v.north };
    draw();
    updateColorbar();
    return;
  }
  if (pending) pending.abort();
  pending = new AbortController();
  try {
    const r = await fetch(url, { signal: pending.signal });
    // Server returns a PNG (dimensions baked into the file). createImageBitmap
    // decodes off the main thread.
    const bitmap = await createImageBitmap(await r.blob());
    renderCache.set(url, bitmap);
    while (renderCache.size > RENDER_CACHE_LIMIT) {
      const oldestKey = renderCache.keys().next().value;
      const evicted = renderCache.get(oldestKey);
      renderCache.delete(oldestKey);
      evicted?.close?.();                            // free GPU memory backing the bitmap
    }
    last = { bitmap, west: v.west, east: v.east, south: v.south, north: v.north };
    draw();
    updateColorbar();
  } catch (e) { if (e.name !== 'AbortError') console.error(e); }
}

// ── colour bar ────────────────────────────────────────────────────────────
function updateColorbar() {
  const cb = document.getElementById('cbar'), n = 200;
  cb.width = 22; cb.height = n;
  const c = cb.getContext('2d'), stops = CFG.vars[curVar].stops;
  for (let i = 0; i < n; i++) {
    const t = 1 - i / (n - 1);                      // top = high value
    c.fillStyle = lerpColor(stops, t); c.fillRect(0, i, 22, 1);
  }
  // Fixed absolute range from the baked-in config — never rescales on pan/zoom.
  const cfg = CFG.vars[curVar];
  const vmin = cfg.vmin, vmax = cfg.vmax, u = cfg.unit;
  // Midpoint label matches the gradient midpoint: geometric mean for a log scale,
  // arithmetic mean for a linear scale.
  const vmid = cfg.log ? Math.pow(10, (Math.log10(vmin) + Math.log10(vmax)) / 2)
                       : (vmin + vmax) / 2;
  document.getElementById('vmax').textContent = fmt(vmax) + ' ' + u;
  document.getElementById('vmid').textContent = fmt(vmid) + ' ' + u;
  document.getElementById('vmin').textContent = fmt(vmin) + ' ' + u;
}
function fmt(x) { return Math.abs(x) >= 100 ? x.toFixed(0) : x.toFixed(1); }
function lerpColor(stops, t) {
  for (let i = 1; i < stops.length; i++) {
    if (t <= stops[i][0]) {
      const [p0, c0] = stops[i - 1], [p1, c1] = stops[i];
      const f = (t - p0) / (p1 - p0 || 1);
      const m = j => Math.round(c0[j] + f * (c1[j] - c0[j]));
      return `rgb(${m(0)},${m(1)},${m(2)})`;
    }
  }
  const c = stops[stops.length - 1][1]; return `rgb(${c[0]},${c[1]},${c[2]})`;
}

// ── interaction: left-drag pan, wheel zoom, hover read-out ──────────────────
canvas.addEventListener('contextmenu', e => e.preventDefault());
let drag = null;
canvas.addEventListener('pointerdown', e => {
  if (e.button !== 0) return;                       // left button pans
  drag = { x: e.clientX, y: e.clientY };
  hideTip();
  canvas.classList.add('panning'); canvas.setPointerCapture(e.pointerId);
});
canvas.addEventListener('pointermove', e => {
  if (drag) {
    const dpr = canvas.width / canvas.clientWidth;
    view.cx -= (e.clientX - drag.x) * dpr * view.dpp;
    view.cy += (e.clientY - drag.y) * dpr * view.dpp;
    drag = { x: e.clientX, y: e.clientY };
    draw(); scheduleRefresh();
  } else {
    onHover(e);
  }
});
function endDrag() { if (drag) { drag = null; canvas.classList.remove('panning'); } }
canvas.addEventListener('pointerup', endDrag);
canvas.addEventListener('pointercancel', endDrag);
canvas.addEventListener('pointerleave', hideTip);

// Hover: throttle a /value lookup at the cursor's lon/lat for the current var.
const tip = document.getElementById('tip');
let hoverLL = null, hoverTimer = null, valAbort = null;
function onHover(e) {
  const dpr = canvas.width / canvas.clientWidth;
  const v = bbox();
  hoverLL = { lon: v.west + e.clientX * dpr * view.dpp,
              lat: v.north - e.clientY * dpr * view.dpp };
  tip.style.left = (e.clientX + 14) + 'px';
  tip.style.top = (e.clientY + 14) + 'px';
  if (!hoverTimer) hoverTimer = setTimeout(fetchValue, 70);
}
async function fetchValue() {
  hoverTimer = null;
  if (!hoverLL) return;
  const { lon, lat } = hoverLL;
  const lonD = ((lon + 180) % 360 + 360) % 360 - 180;   // wrap to [-180,180)
  if (valAbort) valAbort.abort();
  valAbort = new AbortController();
  try {
    const r = await fetch(`/value?var=${curVar}&month=${curMonth}&lon=${lonD}&lat=${lat}`,
                          { signal: valAbort.signal });
    const d = await r.json();
    tip.textContent = d.value == null ? '—'
      : `${curVar === 'pr' ? d.value.toFixed(0) : d.value.toFixed(1)} ${d.unit}`;
    tip.style.display = 'block';
  } catch (err) { /* aborted / network — ignore */ }
}
function hideTip() {
  tip.style.display = 'none'; hoverLL = null;
  if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
}

canvas.addEventListener('wheel', e => {
  e.preventDefault();
  const dpr = canvas.width / canvas.clientWidth;
  const px = e.clientX * dpr, py = e.clientY * dpr;
  const v = bbox();
  const lon = v.west + px * view.dpp, lat = v.north - py * view.dpp;
  const next = clampDpp(view.dpp * Math.exp(e.deltaY * 0.0015));
  view.cx = lon - (px - canvas.width / 2) * next;
  view.cy = lat + (py - canvas.height / 2) * next;
  view.dpp = next;
  draw(); scheduleRefresh();
}, { passive: false });

// ── controls ───────────────────────────────────────────────────────────────
const monthSlider = document.getElementById('month');
const monthTicks = document.getElementById('monthTicks');
const monthList = document.getElementById('monthticks');
// One labelled notch per month: a datalist option (tick mark on the range) plus
// a positioned label centred under each notch.
CFG.months.forEach((name, i) => {
  const o = document.createElement('option'); o.value = i; monthList.appendChild(o);
  const s = document.createElement('span'); s.textContent = name; s.dataset.m = i;
  s.style.left = (i / 11 * 100) + '%';
  s.onclick = () => setMonth(i);
  monthTicks.appendChild(s);
});
function setMonth(m) {
  curMonth = ((m % 12) + 12) % 12;
  monthSlider.value = curMonth;
  monthTicks.querySelectorAll('span').forEach(s =>
    s.classList.toggle('active', +s.dataset.m === curMonth));
  return refresh();
}
monthSlider.addEventListener('input', e => setMonth(+e.target.value));
monthTicks.querySelector('span[data-m="0"]').classList.add('active');  // initial

const playBtn = document.getElementById('play');
// Advance only after the previous month's tile has been fetched + decoded, so on
// slow links the animation paces itself instead of queueing aborts.
playBtn.addEventListener('click', async () => {
  if (playing) { playing = false; playBtn.textContent = '▶'; return; }
  playing = true;
  playBtn.textContent = '⏸';
  while (playing) {
    await setMonth(curMonth + 1);
  }
});

const varsBox = document.getElementById('vars');
CFG.order.forEach(k => {
  const b = document.createElement('button');
  b.textContent = CFG.vars[k].label; b.dataset.var = k;
  if (k === curVar) b.classList.add('active');
  b.onclick = () => {
    curVar = k;
    varsBox.querySelectorAll('button').forEach(x =>
      x.classList.toggle('active', x.dataset.var === k));
    setTitle(); refresh();
  };
  varsBox.appendChild(b);
});
const coastToggle = document.getElementById('coastToggle');
// Toggling coastlines also flips ocean masking, which is server-side, so refetch.
coastToggle.addEventListener('change', e => { showCoast = e.target.checked; refresh(); });

function setTitle() {
  document.getElementById('title').textContent =
    CFG.vars[curVar].label + ' — CHELSA ' + '1981-2010' + ' climatology';
}

function resetView() {
  sizeCanvas();
  // Math.min so the view bbox sits INSIDE the world extent — only one copy is
  // visible. Math.max would have made the whole world fit and left the spare
  // axis padded with wrap copies of the data, which the user finds confusing.
  view.dpp = clampDpp(Math.min(WORLD_W / canvas.width, WORLD_H / canvas.height));
  view.cx = (L + R) / 2; view.cy = (T + B) / 2;
}
window.addEventListener('resize', () => { sizeCanvas(); draw(); scheduleRefresh(); });

// expose a little state for debugging / automated checks
window.__viewer = { view, bbox, setMonth, draw, get curVar() { return curVar; } };

resetView(); setTitle(); refresh(); loadCoastlines();
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if not tif_path("tas", 1).exists():
        print("No data found — run: uv run scripts/download_data.py")
        return 1

    # Build the page once with the server config baked in.
    global PAGE
    PAGE = PAGE.replace("__CONFIG__", _config_json())

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"CHELSA viewer on http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
