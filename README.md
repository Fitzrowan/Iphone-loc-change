# Phone Location Simulator

A local web UI for simulating your iPhone's GPS location by clicking on a world map.
Built on top of `pymobiledevice3`, which uses Apple's developer-mode services (the
same ones Xcode uses) to simulate location over USB — no jailbreak, no paid tools.

## Requirements

- Python 3.8+
- An iPhone with a Lightning/USB-C cable
- `pip install -r requirements.txt`

## One-time device setup

1. Connect your iPhone via USB and unlock it.
2. Tap "Trust This Computer" on the phone when prompted.
3. Pair the device:
   ```
   pymobiledevice3 lockdown pair
   ```
4. Mount the Developer Disk Image (unlocks developer services):
   ```
   pymobiledevice3 mounter auto-mount
   ```
   This needs a `DeveloperDiskImage` matching your iOS version. `pymobiledevice3`
   can often fetch this automatically; if not, search for community-maintained
   `DeveloperDiskImage` repos on GitHub for your iOS version.

## Running the app

**Important: run this from an elevated (Administrator) terminal.** Starting
the iOS 17+ tunnel requires admin privileges on Windows.

```
cd phone-locator
python app.py
```

Then open **http://127.0.0.1:5050** in your browser. The header will show a
yellow "starting tunnel" message for a few seconds, then turn green once
ready. If it turns red, check the message — it usually means the phone isn't
unlocked/connected, or the terminal isn't elevated.

- Click anywhere on the map (or search a place name) to drop a pin.
- Click "Set Location on Phone" to simulate that GPS position.
- Click "Clear / Reset" to return to real GPS.

## Notes

- The phone must stay connected via USB, and the terminal running `app.py`
  must stay open, for the simulated location to persist.
- iOS updates occasionally break `pymobiledevice3` compatibility — check its
  GitHub repo (actively maintained) if commands start failing.
- This affects system-wide location the same way Xcode's built-in location
  simulator does.
- If the tunnel won't start, try running `python -m pymobiledevice3 lockdown
  start-tunnel` directly in an elevated terminal first to confirm it works
  standalone before troubleshooting the app.

## Driving to a place

1. Set a starting location with "Set Location on Phone".
2. Click your destination on the map to drop a pin.
3. Pick a speed and press "Drive to selected point".

The app asks the public OSRM routing server for a driving route, spaces points
along it at your chosen speed (one per second), and plays them to the phone
with `pymobiledevice3 developer dvt simulate-location play`. The drive runs in
real time, so a 60 km trip at 80 km/h takes 45 minutes. "Stop driving" ends it
where you are; "Clear / Reset" returns to real GPS. The computer needs internet
access for route lookups, and the terminal running `app.py` must stay open for
the whole drive.

### Speed limits and stops

By default the drive matches local speed limits and stops at intersections.
Road data comes from OpenStreetMap through the public Overpass API, so the
computer needs internet access and the lookup can take up to a minute.

- Posted limits are used where OpenStreetMap has them. Roads without a posted
  limit get an estimate from the road type (the page shows how much of the
  route had real limits).
- Stop signs on the route always stop for 3 seconds. Traffic lights are red
  60% of the time, for 8 to 35 seconds. Change `LIGHT_STOP_CHANCE`,
  `LIGHT_WAIT_RANGE` and `STOP_SIGN_WAIT` near the top of the driving section
  of `app.py` to adjust this (set `LIGHT_STOP_CHANCE = 1.0` to stop at every light).
- The car accelerates and brakes gradually and slows for turns.
- Choosing "Max N km/h" caps the speed instead of following limits.
- Routes over 120 km, or a failed road-data lookup, fall back to a constant
  speed with no stops, and the page says so.
- Red dots on the map are traffic lights, yellow dots are stop signs.

## Security notes

- The server only listens on `127.0.0.1` and refuses requests whose `Host` or
  `Origin` is not this app's own page, so other websites you visit cannot send
  commands to it. Do not change the host to `0.0.0.0` or expose the port to a
  network: the app runs commands on your computer and may be running as
  Administrator.
- Every coordinate is validated before it is passed to `pymobiledevice3`, and
  commands are always run as a fixed argument list (never through a shell).
- The map library is loaded from a CDN with a pinned version and an integrity
  hash. For fully offline use, download Leaflet and serve it locally.
- Route and drive data is held in temporary files that are deleted when a
  drive ends or is stopped.
- Privacy: this app sends data to third-party services. Your start point and
  destination go to the OSRM routing server and Overpass API (OpenStreetMap),
  place searches go to Nominatim, and map tiles come from openstreetmap.org.
  Nothing is sent to anyone else, and nothing is stored on disk permanently.
- Run `pip install -r requirements.txt` rather than unpinned installs, and keep
  `pymobiledevice3` up to date, since iOS updates regularly break it.

## Disclaimer

For development, testing, and privacy research on your own device. Spoofing
location can violate the terms of service of some apps and games (and can get
accounts banned), and using it to deceive others or evade rules may be illegal.
You are responsible for how you use it. Provided as is, without warranty.
