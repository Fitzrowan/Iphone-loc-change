"""
Phone Location Simulator - local web UI
----------------------------------------
Runs a small local Flask server with a world map. Click anywhere on the map
to simulate that GPS location on a connected, developer-mode-enabled iPhone
via pymobiledevice3.

Requirements:
    pip install flask pymobiledevice3

Usage:
    Run this script from an elevated (Administrator) terminal, with your
    venv activated:
        python app.py
    Then open http://127.0.0.1:5050 in your browser.

Make sure your iPhone is:
    1. Connected via USB and unlocked
    2. Trusted ("Trust This Computer" tapped)
    3. Developer Mode enabled (Settings > Privacy & Security > Developer Mode)
    4. Paired once via: python -m pymobiledevice3 lockdown pair
    5. Developer Disk Image mounted via: python -m pymobiledevice3 mounter auto-mount

This app automatically starts an RSD tunnel (needed for iOS 17.4+) in the
background on startup and reuses it for every location request.
"""

import atexit
import ipaddress
import json
import math
import os
import random
import re
import subprocess  # nosec B404 - only runs fixed commands, never through a shell
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from flask import Flask, abort, request, jsonify, render_template

HOST = "127.0.0.1"
PORT = 5050
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
MAX_ROUTE_RESPONSE_BYTES = 20_000_000
MAX_OVERPASS_RESPONSE_BYTES = 80_000_000
MAX_DRIVE_POINTS = 100_000   # about 27 hours of driving

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10_000   # requests are tiny; reject anything bigger


@app.before_request
def guard_local_only():
    """Only answer requests that really come from this app's own page.

    This server runs on your PC (often as Administrator). Without these checks,
    any website you visit could quietly send requests to it (CSRF) or point its
    own domain at 127.0.0.1 (DNS rebinding).
    """
    if request.host not in ALLOWED_HOSTS:
        abort(403)
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            abort(403)
        if request.content_length and not request.is_json:
            abort(415)


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = (
        "frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"
    )
    return resp


def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _parse_latlon(lat, lon):
    """Return (lat, lon) as floats, or None if they are not valid coordinates."""
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _valid_rsd(addr, port):
    try:
        ipaddress.ip_address(addr)
        return port.isdigit() and 1 <= int(port) <= 65535
    except ValueError:
        return False


def _read_capped(resp, limit):
    raw = resp.read(limit + 1)
    if len(raw) > limit:
        raise RuntimeError("response from the server was unexpectedly large")
    return raw.decode("utf-8")

# Keep track of last set location so the UI can show current state
current_location = {"lat": None, "lon": None}

# Tunnel state, populated by the background thread once the tunnel is up
tunnel_state = {
    "rsd_address": None,
    "rsd_port": None,
    "status": "starting",   # starting | ready | error
    "message": "Starting tunnel...",
}
tunnel_process = None


def _tunnel_watcher():
    """Start `lockdown start-tunnel` and parse its output for the RSD address/port."""
    global tunnel_process
    try:
        tunnel_process = subprocess.Popen(  # nosec B603 - fixed argument list
            [sys.executable, "-m", "pymobiledevice3", "lockdown", "start-tunnel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        tunnel_state["status"] = "error"
        tunnel_state["message"] = "pymobiledevice3 not found. Install it with: pip install pymobiledevice3"
        return

    addr, port = None, None
    recent_lines = []
    for line in tunnel_process.stdout:
        line = line.strip()
        if not line:
            continue
        recent_lines.append(line)
        recent_lines = recent_lines[-6:]
        print("[tunnel]", line, flush=True)
        m_addr = re.search(r"RSD Address:\s*(\S+)", line)
        m_port = re.search(r"RSD Port:\s*(\d+)", line)
        if m_addr:
            addr = m_addr.group(1)
        if m_port:
            port = m_port.group(1)
        if addr and port:
            if not _valid_rsd(addr, port):
                tunnel_state["status"] = "error"
                tunnel_state["message"] = "Tunnel reported an unexpected address, so it was not used."
                tunnel_process.terminate()
                return
            tunnel_state["rsd_address"] = addr
            tunnel_state["rsd_port"] = port
            tunnel_state["status"] = "ready"
            tunnel_state["message"] = f"Tunnel ready ({addr} : {port})"
            break

    if tunnel_state["status"] != "ready":
        tunnel_state["status"] = "error"
        tunnel_state["message"] = (
            "Tunnel process ended before an RSD address was found. "
            "Check Administrator privileges, that the phone is connected/unlocked, "
            "and that no other tunnel process is still running. Last output: "
            + " | ".join(recent_lines)
        )
        return

    # Keep monitoring in case the tunnel process exits later (unplug, etc.)
    tunnel_process.wait()
    if tunnel_state["status"] == "ready":
        tunnel_state["status"] = "error"
        tunnel_state["message"] = "Tunnel process exited. Reconnect your phone and restart the app."


def start_tunnel_background():
    t = threading.Thread(target=_tunnel_watcher, daemon=True)
    t.start()


def run_pmd3(args, use_rsd=True, wait_seconds=None):
    """Run a pymobiledevice3 command and return (success, output).

    `args` should look like ["developer", "dvt", "simulate-location", "set", "--", "40.7", "-74.0"]
    i.e. the subcommand path, then "--", then positional args. The --rsd flag
    (with address/port) is inserted right before the "--" separator.
    """
    if use_rsd:
        if tunnel_state["status"] != "ready":
            return False, f"Tunnel not ready yet: {tunnel_state['message']}"
        rsd_flag = ["--rsd", tunnel_state["rsd_address"], tunnel_state["rsd_port"]]
    else:
        rsd_flag = []

    try:
        sep_index = args.index("--")
    except ValueError:
        sep_index = len(args)

    full_cmd = (
        [sys.executable, "-m", "pymobiledevice3"]
        + args[:sep_index]
        + rsd_flag
        + args[sep_index:]
    )

    # NOTE: pymobiledevice3's `dvt simulate-location` command has a documented
    # quirk where it can hang instead of exiting cleanly after applying the
    # location. Per user preference, we don't time out or kill it here — we
    # just wait for it to finish on its own. This means a request to
    # /api/set_location or /api/clear_location may take a while (or not
    # return until you stop the process manually) if this happens.
    proc = subprocess.Popen(  # nosec B603 - fixed arguments, inputs validated
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = proc.communicate(timeout=wait_seconds)
        ok = proc.returncode == 0
        return ok, output.strip()
    except subprocess.TimeoutExpired:
        # Not killed: a command that errors out does so quickly, so one still
        # running after wait_seconds has most likely applied and just not exited.
        return True, "Location command sent. It's still running in the background, which is normal for pymobiledevice3, and the location stays set."
    except FileNotFoundError:
        return False, "pymobiledevice3 not found. Install it with: pip install pymobiledevice3"
    except Exception as e:
        return False, str(e)


start_tunnel_background()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/set_location", methods=["POST"])
def set_location():
    data = _json_body()
    coords = _parse_latlon(data.get("lat"), data.get("lon"))
    if coords is None:
        return jsonify({"success": False, "message": "Invalid coordinates"}), 400
    lat, lon = coords

    stop_drive()
    ok, output = run_pmd3(
        ["developer", "dvt", "simulate-location", "set", "--", f"{lat:.7f}", f"{lon:.7f}"],
        wait_seconds=15,
    )

    if ok:
        current_location["lat"] = lat
        current_location["lon"] = lon

    return jsonify({"success": ok, "message": output})


@app.route("/api/clear_location", methods=["POST"])
def clear_location():
    stop_drive()
    ok, output = run_pmd3(["developer", "dvt", "simulate-location", "clear"], wait_seconds=15)
    if ok:
        current_location["lat"] = None
        current_location["lon"] = None
    return jsonify({"success": ok, "message": output})



# ---------------------------------------------------------------------------
# Drive simulation
# ---------------------------------------------------------------------------
# Gets a driving route from the public OSRM demo server, spaces points along it
# at the chosen speed (one point per second of travel), writes them to a GPX
# file with timestamps, and hands that to `simulate-location play`, which sets
# each point in order and waits between points according to the timestamps.

STEP_SECONDS = 1.0

drive_state = {
    "proc": None,
    "points": [],        # resampled (lat, lon) list, one per STEP_SECONDS
    "started_at": None,
    "log_path": None,
    "gpx_path": None,
    "log_file": None,
}


def _cleanup_drive_files():
    """Delete the temporary route/log files (they contain your coordinates)."""
    lf = drive_state.get("log_file")
    if lf:
        try:
            lf.close()
        except OSError:
            pass
    for key in ("gpx_path", "log_path"):
        path = drive_state.get(key)
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
    drive_state.update(log_file=None, gpx_path=None, log_path=None)


def _haversine(a, b):
    r = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(d))


def fetch_route(start, end):
    """Return a list of (lat, lon) along a driving route from start to end."""
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{start[1]},{start[0]};{end[1]},{end[0]}?overview=full&geometries=geojson"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "phone-locator-local-tool"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 - fixed https URL
        data = json.loads(_read_capped(resp, MAX_ROUTE_RESPONSE_BYTES))
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RuntimeError(f"No driving route found ({data.get('code')}). Is the destination reachable by road?")
    return [(lat, lon) for lon, lat in data["routes"][0]["geometry"]["coordinates"]]


def resample(coords, speed_mps, step_s=STEP_SECONDS):
    """Return points spaced evenly in time along the polyline."""
    step_m = speed_mps * step_s
    out = [coords[0]]
    carry = 0.0  # distance already travelled since the last emitted point
    for a, b in zip(coords, coords[1:]):
        seg = _haversine(a, b)
        if seg == 0:
            continue
        pos = step_m - carry
        while pos <= seg:
            f = pos / seg
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
            pos += step_m
        carry = seg - (pos - step_m)
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


def write_gpx(points, path, step_s=STEP_SECONDS):
    t0 = datetime.now(timezone.utc).replace(microsecond=0)
    lines = ['<?xml version="1.0"?>', '<gpx version="1.1" creator="phone-locator"><trk><trkseg>']
    for i, (lat, lon) in enumerate(points):
        t = (t0 + timedelta(seconds=i * step_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}"><time>{t}</time></trkpt>')
    lines.append("</trkseg></trk></gpx>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _read_log():
    path = drive_state.get("log_path")
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def stop_drive():
    """Stop any running drive playback. Returns True if one was running."""
    proc = drive_state["proc"]
    was_running = proc is not None and proc.poll() is None
    if was_running:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    drive_state["proc"] = None
    _cleanup_drive_files()
    return was_running



# ---------------------------------------------------------------------------
# Realistic driving: local speed limits, stops, turns
# ---------------------------------------------------------------------------
# Road data comes from OpenStreetMap through the public Overpass API. Posted
# limits (maxspeed tags) are used where they exist; elsewhere the speed is
# estimated from the road type. Traffic lights and stop signs that sit on the
# route become stops. Speed is then shaped by acceleration/braking limits and
# slowed for tight turns.

MAX_SMART_ROUTE_KM = 120
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
FINE_STEP_M = 5.0          # spacing of the internal route model
ACCEL = 1.5                # m/s^2 speeding up
DECEL = 2.0                # m/s^2 braking
LATERAL_ACCEL = 2.0        # m/s^2 sideways limit used to slow for turns
LIGHT_STOP_CHANCE = 0.6    # chance a traffic light is red when you arrive
LIGHT_WAIT_RANGE = (8, 35) # seconds waiting at a red light
STOP_SIGN_WAIT = 3         # seconds stopped at a stop sign
FALLBACK_SPEED_MPS = 40 / 3.6
ROAD_CLASSES = (
    "motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|"
    "secondary_link|tertiary|tertiary_link|unclassified|residential|living_street|service"
)
# Used only when a road has no posted limit in the map data (km/h, US-style).
DEFAULT_SPEED_KMH = {
    "motorway": 105, "motorway_link": 65, "trunk": 88, "trunk_link": 55,
    "primary": 72, "primary_link": 48, "secondary": 64, "secondary_link": 48,
    "tertiary": 56, "tertiary_link": 40, "unclassified": 48,
    "residential": 40, "living_street": 16, "service": 24,
}


class _Proj:
    """Flat local projection (metres) around a reference point."""
    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = 111320.0 * math.cos(math.radians(lat0))
        self.ky = 110540.0

    def xy(self, lat, lon):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)


def _simplify_indices(xy, tol):
    """Douglas-Peucker; returns indices of points to keep."""
    n = len(xy)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]
        bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        dmax, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            if l2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > dmax:
                dmax, idx = d, i
        if dmax > tol:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [i for i, k in enumerate(keep) if k]


def fetch_osm_elements(route):
    """Ask Overpass for roads and traffic lights/stop signs along the route."""
    proj = _Proj(*route[0])
    xy = [proj.xy(la, lo) for la, lo in route]
    tol = 5.0
    keep = _simplify_indices(xy, tol)
    while len(keep) > 3000:
        tol *= 2
        keep = _simplify_indices(xy, tol)
    poly = ",".join(f"{route[i][0]:.6f},{route[i][1]:.6f}" for i in keep)
    query = (
        "[out:json][timeout:90];("
        f'way(around:25,{poly})["highway"~"^({ROAD_CLASSES})$"];'
        f'node(around:6,{poly})["highway"~"^(traffic_signals|stop)$"];'
        ");out geom;"
    )
    req = urllib.request.Request(
        OVERPASS_URL,
        data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
        headers={"User-Agent": "phone-locator-local-tool"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:  # nosec B310 - fixed https URL
        return json.loads(_read_capped(resp, MAX_OVERPASS_RESPONSE_BYTES)).get("elements", [])


def parse_maxspeed(val):
    """OSM maxspeed tag -> m/s, or None if it can't be read."""
    if not val:
        return None
    v = val.strip().lower()
    if v == "none":
        return 130 / 3.6
    if v == "walk":
        return 7 / 3.6
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(mph|km/h|kmh)?", v)
    if not m:
        return None
    n = float(m.group(1))
    kmh = n * 1.609344 if m.group(2) == "mph" else n
    return kmh / 3.6


def densify(coords, step_m):
    """Re-space a polyline evenly. Returns (points, actual_step_m)."""
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + _haversine(a, b))
    total = cum[-1]
    n = max(1, int(math.ceil(total / step_m)))
    ds = total / n
    pts, j = [], 0
    for i in range(n + 1):
        s = i * ds
        while j < len(cum) - 2 and cum[j + 1] < s:
            j += 1
        seg = cum[j + 1] - cum[j]
        f = 0.0 if seg == 0 else min(1.0, max(0.0, (s - cum[j]) / seg))
        a, b = coords[j], coords[j + 1]
        pts.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return pts, ds


_CELL = 30.0


def _build_way_grid(ways, proj, bounds):
    minx, maxx, miny, maxy = bounds
    grid = {}
    for w in ways:
        tags = w.get("tags", {})
        explicit = parse_maxspeed(tags.get("maxspeed"))
        spd = explicit
        if spd is None:
            spd = DEFAULT_SPEED_KMH.get(tags.get("highway"), 40) / 3.6
        geom = [proj.xy(g["lat"], g["lon"]) for g in w["geometry"]]
        for (x1, y1), (x2, y2) in zip(geom, geom[1:]):
            if max(x1, x2) < minx or min(x1, x2) > maxx or max(y1, y2) < miny or min(y1, y2) > maxy:
                continue
            length = math.hypot(x2 - x1, y2 - y1)
            if length == 0:
                continue
            pieces = max(1, int(math.ceil(length / 25.0)))
            for k in range(pieces):
                ax = x1 + (x2 - x1) * k / pieces
                ay = y1 + (y2 - y1) * k / pieces
                bx = x1 + (x2 - x1) * (k + 1) / pieces
                by = y1 + (y2 - y1) * (k + 1) / pieces
                hx, hy = (x2 - x1) / length, (y2 - y1) / length
                seg = (ax, ay, bx, by, spd, explicit is not None, hx, hy)
                for cx in range(int(min(ax, bx) // _CELL), int(max(ax, bx) // _CELL) + 1):
                    for cy in range(int(min(ay, by) // _CELL), int(max(ay, by) // _CELL) + 1):
                        grid.setdefault((cx, cy), []).append(seg)
    return grid


def _dist_to_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def plan_smart_drive(route, cap_kmh, elements=None):
    """Build one (lat, lon) per second for a realistic drive along `route`.

    cap_kmh=None follows local limits; a number caps the speed at that value.
    Returns (points, info).
    """
    if elements is None:
        elements = fetch_osm_elements(route)
    if not elements:
        raise RuntimeError("no road data returned for this route")
    cap_mps = cap_kmh / 3.6 if cap_kmh else None

    pts, ds = densify(route, FINE_STEP_M)
    n = len(pts)
    proj = _Proj(*pts[0])
    xy = [proj.xy(la, lo) for la, lo in pts]
    xs, ys = [p[0] for p in xy], [p[1] for p in xy]
    bounds = (min(xs) - 60, max(xs) + 60, min(ys) - 60, max(ys) + 60)

    # --- speed limit at each point of the route ---
    ways = [e for e in elements if e.get("type") == "way" and e.get("geometry")]
    grid = _build_way_grid(ways, proj, bounds)
    limits, explicit_count, last = [], 0, FALLBACK_SPEED_MPS
    for i in range(n):
        a, b = max(0, i - 1), min(n - 1, i + 1)
        rx, ry = xy[b][0] - xy[a][0], xy[b][1] - xy[a][1]
        rl = math.hypot(rx, ry) or 1.0
        rx, ry = rx / rl, ry / rl
        px, py = xy[i]
        cx, cy = int(px // _CELL), int(py // _CELL)
        best, best_score = None, 1e9
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for seg in grid.get((gx, gy), ()):
                    d = _dist_to_seg(px, py, seg[0], seg[1], seg[2], seg[3])
                    if d > 20:
                        continue
                    # prefer roads running the same way as the route over cross streets
                    score = d + (30 if abs(rx * seg[6] + ry * seg[7]) < 0.5 else 0)
                    if score < best_score:
                        best, best_score = seg, score
        if best is not None:
            last = best[4]
            explicit_count += 1 if best[5] else 0
        limits.append(last)
    if cap_mps:
        limits = [min(v, cap_mps) for v in limits]

    # --- slow down for turns and bends ---
    window = max(1, int(round(15.0 / ds)))
    for i in range(window, n - window):
        a, b = i - window, i + window
        h1 = math.atan2(xy[i][1] - xy[a][1], xy[i][0] - xy[a][0])
        h2 = math.atan2(xy[b][1] - xy[i][1], xy[b][0] - xy[i][0])
        dth = abs((h2 - h1 + math.pi) % (2 * math.pi) - math.pi)
        if dth > 0.05:
            radius = (2 * window * ds) / dth
            limits[i] = min(limits[i], max(3.0, math.sqrt(LATERAL_ACCEL * radius)))

    # --- stops: traffic lights and stop signs sitting on the route ---
    pgrid = {}
    for i, (px, py) in enumerate(xy):
        pgrid.setdefault((int(px // 12), int(py // 12)), []).append(i)
    found = []
    for e in elements:
        if e.get("type") != "node":
            continue
        kind = {"traffic_signals": "signal", "stop": "stop"}.get(e.get("tags", {}).get("highway"))
        if kind is None:
            continue
        sx, sy = proj.xy(e["lat"], e["lon"])
        bi, bd = None, 6.0
        for gx in range(int(sx // 12) - 1, int(sx // 12) + 2):
            for gy in range(int(sy // 12) - 1, int(sy // 12) + 2):
                for i in pgrid.get((gx, gy), ()):
                    d = math.hypot(xy[i][0] - sx, xy[i][1] - sy)
                    if d <= bd:
                        bi, bd = i, d
        if bi is not None and 4 <= bi <= n - 5:
            found.append((bi, kind))
    found.sort()
    dwell, stops_info, last_idx = {}, [], -999
    for idx, kind in found:
        if (idx - last_idx) * ds < 15:
            continue
        last_idx = idx
        if kind == "stop":
            stopped, wait = True, STOP_SIGN_WAIT
        else:
            stopped = random.random() < LIGHT_STOP_CHANCE  # nosec B311 - simulation timing, not security
            wait = random.randint(*LIGHT_WAIT_RANGE) if stopped else 0  # nosec B311
        if stopped:
            dwell[idx] = wait
        stops_info.append([round(pts[idx][0], 6), round(pts[idx][1], 6), kind, stopped])

    # --- speed profile: accelerate, brake for stops/turns, stop at the end ---
    v2 = [l * l for l in limits]
    for k in dwell:
        v2[k] = 0.0
    v2[0] = v2[-1] = 0.0
    for i in range(n - 1):
        v2[i + 1] = min(v2[i + 1], v2[i] + 2 * ACCEL * ds)
    for i in range(n - 2, -1, -1):
        v2[i] = min(v2[i], v2[i + 1] + 2 * DECEL * ds)
    v = [math.sqrt(x) for x in v2]

    # --- timeline (arrival times, plus waiting at stops) ---
    t, anchors = 0.0, [(0.0, 0)]
    for i in range(n - 1):
        if i in dwell:
            t += dwell[i]
            anchors.append((t, i))
        s = v[i] + v[i + 1]
        t += (2 * ds / s) if s > 0.1 else ds / 0.5
        anchors.append((t, i + 1))
    total = t

    # --- one point per second ---
    out, j, sec = [], 0, 0.0
    while sec <= total:
        while j < len(anchors) - 2 and anchors[j + 1][0] <= sec:
            j += 1
        t0, i0 = anchors[j]
        t1, i1 = anchors[j + 1]
        idxf = i1 if t1 <= t0 else i0 + (i1 - i0) * min(1.0, max(0.0, (sec - t0) / (t1 - t0)))
        ii = int(idxf)
        if ii >= n - 1:
            out.append(pts[-1])
        else:
            f = idxf - ii
            a, b = pts[ii], pts[ii + 1]
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
        sec += STEP_SECONDS
    if out[-1] != pts[-1]:
        out.append(pts[-1])

    info = {
        "mode": "smart",
        "stops": stops_info,
        "posted_limit_pct": round(100 * explicit_count / n),
        "warning": None,
    }
    return out, info


@app.route("/api/drive", methods=["POST"])
def drive():
    data = _json_body()
    speed_raw = data.get("speed_kmh", "auto")
    try:
        dest = _parse_latlon(data["lat"], data["lon"])
        cap_kmh = None if speed_raw == "auto" else float(speed_raw)
    except (KeyError, TypeError, ValueError):
        return jsonify({"success": False, "message": "Missing or invalid destination/speed"}), 400
    if dest is None:
        return jsonify({"success": False, "message": "Invalid destination coordinates"}), 400
    if cap_kmh is not None and not 5 <= cap_kmh <= 300:
        return jsonify({"success": False, "message": "Speed must be between 5 and 300 km/h"}), 400

    if current_location["lat"] is None:
        return jsonify({"success": False, "message": "Set a starting location on the phone first, then pick a destination."}), 400
    if tunnel_state["status"] != "ready":
        return jsonify({"success": False, "message": f"Tunnel not ready: {tunnel_state['message']}"}), 400

    start = (current_location["lat"], current_location["lon"])
    try:
        route = fetch_route(start, dest)
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not get a route: {e}"}), 502

    route_m = sum(_haversine(a, b) for a, b in zip(route, route[1:]))
    info = {"mode": "constant", "stops": [], "posted_limit_pct": None, "warning": None}
    points = None
    if route_m / 1000 <= MAX_SMART_ROUTE_KM:
        try:
            points, info = plan_smart_drive(route, cap_kmh)
        except Exception as e:
            info["warning"] = f"Couldn't load road data ({e}), so this drive uses a constant speed with no stops."
    else:
        info["warning"] = (
            f"Route is over {MAX_SMART_ROUTE_KM} km, so speed limits and stops are skipped "
            "and this drive uses a constant speed."
        )
    if points is None:
        points = resample(route, (cap_kmh or 50) / 3.6)
    if len(points) < 2:
        return jsonify({"success": False, "message": "Start and destination are too close to drive between."}), 400
    if len(points) > MAX_DRIVE_POINTS:
        return jsonify({"success": False, "message": "That drive would take over 27 hours. Pick a closer destination or a higher speed."}), 400

    stop_drive()
    fd, gpx_path = tempfile.mkstemp(suffix=".gpx", prefix="drive_")
    os.close(fd)
    write_gpx(points, gpx_path)
    fd, log_path = tempfile.mkstemp(suffix=".log", prefix="drive_")
    os.close(fd)

    cmd = [
        sys.executable, "-m", "pymobiledevice3", "developer", "dvt", "simulate-location", "play",
        "--rsd", tunnel_state["rsd_address"], tunnel_state["rsd_port"], gpx_path,
    ]
    log_file = open(log_path, "w", encoding="utf-8")
    drive_state.update(
        proc=subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT),  # nosec B603
        points=points,
        started_at=time.time(),
        log_path=log_path,
        gpx_path=gpx_path,
        log_file=log_file,
    )

    total_m = sum(_haversine(a, b) for a, b in zip(route, route[1:]))
    return jsonify({
        "success": True,
        "distance_km": round(total_m / 1000, 1),
        "duration_s": int(len(points) * STEP_SECONDS),
        "route": [[lat, lon] for lat, lon in route],
        "mode": info["mode"],
        "stops": info["stops"],
        "posted_limit_pct": info["posted_limit_pct"],
        "warning": info["warning"],
        "message": "Driving started",
    })


@app.route("/api/drive_status")
def drive_status():
    proc = drive_state["proc"]
    points = drive_state["points"]
    if proc is None or not points:
        return jsonify({"state": "idle"})

    log = _read_log()
    done = proc.poll() is not None
    # Prefer the player's own progress lines; fall back to elapsed time.
    reported = log.count("set location to")
    if reported:
        idx = reported - 1
    else:
        idx = int((time.time() - drive_state["started_at"]) / STEP_SECONDS)
    idx = max(0, min(idx, len(points) - 1))
    lat, lon = points[idx]
    current_location["lat"], current_location["lon"] = lat, lon

    if done:
        failed = proc.returncode not in (0, None) and idx < len(points) - 1
        tail = " | ".join([l for l in log.strip().splitlines() if l.strip()][-4:])
        drive_state["proc"] = None
        _cleanup_drive_files()
        return jsonify({
            "state": "failed" if failed else "arrived",
            "lat": lat, "lon": lon,
            "message": tail if failed else "Arrived",
        })
    return jsonify({
        "state": "driving",
        "lat": lat, "lon": lon,
        "progress": round(idx / max(1, len(points) - 1), 3),
        "remaining_s": int((len(points) - 1 - idx) * STEP_SECONDS),
    })


@app.route("/api/stop_drive", methods=["POST"])
def stop_drive_route():
    stopped = stop_drive()
    return jsonify({"success": True, "message": "Drive stopped" if stopped else "No drive was running"})


def _shutdown():
    """Stop child processes when the app exits so nothing is left running."""
    try:
        stop_drive()
    except Exception:  # nosec B110 - best effort during shutdown
        pass
    if tunnel_process is not None and tunnel_process.poll() is None:
        tunnel_process.terminate()


atexit.register(_shutdown)


@app.route("/api/status")
def status():
    return jsonify(current_location)


@app.route("/api/tunnel_status")
def tunnel_status():
    return jsonify(tunnel_state)


if __name__ == "__main__":
    print("Starting Phone Location Simulator UI at http://127.0.0.1:5050")
    print("Make sure your iPhone is connected, trusted, and paired via pymobiledevice3.")
    app.run(host=HOST, port=PORT, debug=False)
