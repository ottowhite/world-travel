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
    GET /render?var=&month=&west=&east=&south=&north=&w=&h=
                                    raw RGBA bytes (w*h*4); X-Vmin/X-Vmax/X-Width
                                    /X-Height headers carry the colour range used
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
from rasterio.windows import from_bounds

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PERIOD = "1981-2010"
VERSION = "V.2.1"
SCALE = 0.1  # CHELSA V2.1 DN -> physical, before any offset
MAX_PX = 2200  # cap render size to bound work
MAX_COPIES = 12  # cap wrap tiles per axis

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# value (0..1) -> RGB anchor stops for each variable's colour map.
VARIABLES = {
    "tas": {
        "label": "Temperature", "unit": "°C",
        "convert": lambda k: k - 273.15,  # Kelvin (DN*0.1) -> °C
        "stops": [(0.0, (5, 48, 97)), (0.25, (67, 147, 195)),
                  (0.5, (247, 247, 247)), (0.75, (214, 96, 77)),
                  (1.0, (103, 0, 31))],   # cold blue -> hot red
    },
    "pr": {
        "label": "Precipitation", "unit": "mm / month",
        "convert": lambda mm: mm,
        "stops": [(0.0, (255, 255, 217)), (0.5, (65, 182, 196)),
                  (1.0, (8, 29, 88))],    # dry pale -> wet navy
    },
}


def tif_path(var: str, month: int) -> Path:
    return DATA_DIR / f"CHELSA_{var}_{month:02d}_{PERIOD}_{VERSION}.tif"


@lru_cache(maxsize=None)
def _lut(var: str) -> np.ndarray:
    stops = VARIABLES[var]["stops"]
    xs = [s[0] for s in stops]
    grid = np.linspace(0, 1, 256)
    lut = np.zeros((256, 3), np.uint8)
    for ch in range(3):
        lut[:, ch] = np.interp(grid, xs, [s[1][ch] for s in stops]).astype(np.uint8)
    return lut


@lru_cache(maxsize=1)
def _bounds():
    # All variables/months share the same grid; read it once.
    with rasterio.open(tif_path("tas", 1)) as ds:
        b = ds.bounds
    return b.left, b.bottom, b.right, b.top


def render(var, month, west, east, south, north, w, h):
    """Assemble the view bbox into a (h, w) float array, wrapping on both axes."""
    cfg = VARIABLES[var]
    left, bottom, right, top = _bounds()
    world_w, world_h = right - left, top - bottom
    out = np.full((h, w), np.nan, dtype="float32")
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

    finite = out[np.isfinite(out)]
    if finite.size:
        vmin, vmax = (float(v) for v in np.percentile(finite, [2, 98]))
    else:
        vmin, vmax = 0.0, 1.0
    if vmax <= vmin:
        vmax = vmin + 1.0

    lut = _lut(var)
    mask = np.isfinite(out)
    idx = np.zeros((h, w), np.uint8)
    norm = np.clip((out[mask] - vmin) / (vmax - vmin), 0, 1)
    idx[mask] = (norm * 255).astype(np.uint8)
    rgba = np.empty((h, w, 4), np.uint8)
    rgba[..., :3] = lut[idx]
    rgba[..., 3] = np.where(mask, 255, 0)
    return rgba, vmin, vmax


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # keep the console quiet
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._send_html()
        if url.path == "/render":
            return self._send_render(parse_qs(url.query))
        self.send_error(404)

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
            assert var in VARIABLES and 0 <= month < 12 and w > 0 and h > 0
        except (KeyError, ValueError, AssertionError):
            return self.send_error(400)

        rgba, vmin, vmax = render(var, month, west, east, south, north, w, h)
        body = rgba.tobytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Width", str(w))
        self.send_header("X-Height", str(h))
        self.send_header("X-Vmin", f"{vmin:.4g}")
        self.send_header("X-Vmax", f"{vmax:.4g}")
        self.end_headers()
        self.wfile.write(body)


def _config_json():
    return json.dumps({
        "months": MONTHS,
        "order": list(VARIABLES),
        "vars": {k: {"label": v["label"], "unit": v["unit"],
                     "stops": v["stops"]} for k, v in VARIABLES.items()},
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
  #month { width: 240px; }
  #monthLabel { min-width: 30px; font-weight: 600; }
  #title { position: fixed; top: 10px; right: 14px; color: #ddd; z-index: 10;
           font-size: 14px; text-shadow: 0 1px 2px #000; }
  #cbar { position: fixed; right: 16px; bottom: 18px; width: 22px; height: 200px;
          border: 1px solid #0008; border-radius: 4px; z-index: 10; }
  #cbarWrap { position: fixed; right: 44px; bottom: 18px; height: 200px; z-index: 10;
              display: flex; flex-direction: column; justify-content: space-between;
              color: #eee; font-size: 12px; text-shadow: 0 1px 2px #000; text-align: right; }
  #hint { position: fixed; left: 12px; bottom: 12px; color: #9aa5b1; font-size: 12px;
          z-index: 10; }
</style>
</head>
<body>
<canvas id="map"></canvas>
<div id="bar">
  <div class="g" id="vars"><strong>Variable:</strong></div>
  <div class="g">
    <button id="play">▶</button>
    <input id="month" type="range" min="0" max="11" step="1" value="0"/>
    <span id="monthLabel">Jan</span>
  </div>
</div>
<div id="title"></div>
<div id="cbarWrap"><span id="vmax">–</span><span id="vmid"></span><span id="vmin">–</span></div>
<canvas id="cbar"></canvas>
<div id="hint">right-drag to pan · scroll to zoom · wraps around</div>
<script>
const CFG = __CONFIG__;
const [L, B, R, T] = CFG.bounds;        // source extent (deg)
const WORLD_W = R - L, WORLD_H = T - B;
const canvas = document.getElementById('map');
const ctx = canvas.getContext('2d');

let curVar = CFG.order[0], curMonth = 0;
const view = { cx: 0, cy: (T + B) / 2, dpp: 1 };   // centre lon/lat, deg per backing-px
let last = null;                                    // {bitmap, west, east, south, north}
let pending = null, refreshTimer = null, playTimer = null;

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
}
const MAXK = 4;

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 110);
}

async function refresh() {
  const v = bbox();
  const w = canvas.width, h = canvas.height;
  const url = `/render?var=${curVar}&month=${curMonth}` +
    `&west=${v.west}&east=${v.east}&south=${v.south}&north=${v.north}&w=${w}&h=${h}`;
  if (pending) pending.abort();
  pending = new AbortController();
  try {
    const r = await fetch(url, { signal: pending.signal });
    const W = +r.headers.get('X-Width'), H = +r.headers.get('X-Height');
    const vmin = +r.headers.get('X-Vmin'), vmax = +r.headers.get('X-Vmax');
    const buf = new Uint8ClampedArray(await r.arrayBuffer());
    const img = new ImageData(buf, W, H);
    const bitmap = await createImageBitmap(img);
    last = { bitmap, west: v.west, east: v.east, south: v.south, north: v.north };
    draw();
    updateColorbar(vmin, vmax);
  } catch (e) { if (e.name !== 'AbortError') console.error(e); }
}

// ── colour bar ────────────────────────────────────────────────────────────
function updateColorbar(vmin, vmax) {
  const cb = document.getElementById('cbar'), n = 200;
  cb.width = 22; cb.height = n;
  const c = cb.getContext('2d'), stops = CFG.vars[curVar].stops;
  for (let i = 0; i < n; i++) {
    const t = 1 - i / (n - 1);                      // top = high value
    c.fillStyle = lerpColor(stops, t); c.fillRect(0, i, 22, 1);
  }
  const u = CFG.vars[curVar].unit;
  document.getElementById('vmax').textContent = fmt(vmax) + ' ' + u;
  document.getElementById('vmid').textContent = fmt((vmin + vmax) / 2);
  document.getElementById('vmin').textContent = fmt(vmin);
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

// ── interaction: right-drag pan, wheel zoom ────────────────────────────────
canvas.addEventListener('contextmenu', e => e.preventDefault());
let drag = null;
canvas.addEventListener('pointerdown', e => {
  if (e.button !== 2) return;                       // right button only
  drag = { x: e.clientX, y: e.clientY };
  canvas.classList.add('panning'); canvas.setPointerCapture(e.pointerId);
});
canvas.addEventListener('pointermove', e => {
  if (!drag) return;
  const dpr = canvas.width / canvas.clientWidth;
  view.cx -= (e.clientX - drag.x) * dpr * view.dpp;
  view.cy += (e.clientY - drag.y) * dpr * view.dpp;
  drag = { x: e.clientX, y: e.clientY };
  draw(); scheduleRefresh();
});
function endDrag() { if (drag) { drag = null; canvas.classList.remove('panning'); } }
canvas.addEventListener('pointerup', endDrag);
canvas.addEventListener('pointercancel', endDrag);

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
const monthLabel = document.getElementById('monthLabel');
function setMonth(m) {
  curMonth = ((m % 12) + 12) % 12;
  monthSlider.value = curMonth; monthLabel.textContent = CFG.months[curMonth];
  refresh();
}
monthSlider.addEventListener('input', e => setMonth(+e.target.value));

const playBtn = document.getElementById('play');
playBtn.addEventListener('click', () => {
  if (playTimer) { clearInterval(playTimer); playTimer = null; playBtn.textContent = '▶'; return; }
  playBtn.textContent = '⏸';
  playTimer = setInterval(() => setMonth(curMonth + 1), 900);
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
function setTitle() {
  document.getElementById('title').textContent =
    CFG.vars[curVar].label + ' — CHELSA ' + '1981-2010' + ' climatology';
}

function resetView() {
  sizeCanvas();
  view.dpp = clampDpp(Math.max(WORLD_W / canvas.width, WORLD_H / canvas.height));
  view.cx = (L + R) / 2; view.cy = (T + B) / 2;
}
window.addEventListener('resize', () => { sizeCanvas(); draw(); scheduleRefresh(); });

// expose a little state for debugging / automated checks
window.__viewer = { view, bbox, setMonth, get curVar() { return curVar; } };

resetView(); setTitle(); refresh();
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
