"""
connectors/location.py
Where the machine running Friday is. Stateless apart from a short in-process
cache — nothing is written to SQLite.

Read the name carefully: this reports the location of the *Mac*, not of the
user. Friday lives on an always-on machine, so in practice the answer is
"home" whenever the machine is at home, and it does not move when the user
does. There is no passive way to get a phone's position from here. The tool
docstring in agent/tools.py tells the LLM to phrase answers accordingly.

Two backends, tried in order:

  1. CoreLocation (PyObjC) — Wi-Fi positioning via macOS Location Services.
     Accurate to tens of metres, and the only source that knows anything the
     network doesn't. Requires a TCC grant.
  2. IP geolocation (ipapi.co) — city-level, no key, works everywhere
     including the Windows build. Always available as a floor, which matters
     because CoreLocation is the more fragile of the two by a wide margin.

On the CoreLocation grant: like every other TCC permission in this codebase it
is keyed to the *binary*, not the script. Under the LaunchAgent, friday.py runs
as a bare interpreter with no Info.plist, so `NSLocationWhenInUseUsageDescription`
is absent and authorization resolves to denied without ever prompting. Inside
the packaged .app the key is present (packaging/macos/friday.spec) and the
prompt appears on first call. Both cases degrade to the IP backend rather than
failing, which is why the IP path is not optional.

Synchronous — always wrap calls in run_in_executor inside async handlers.
"""

import logging
import threading
import time

import requests

logger = logging.getLogger("friday.location")

_CACHE_TTL_S = 300.0
_CL_TIMEOUT_S = 15.0
_GEOCODE_TIMEOUT_S = 10.0

_cache_lock = threading.Lock()
_cache = {"at": 0.0, "fix": None}


# ── CoreLocation ─────────────────────────────────────────────────────────────

_cl_lock = threading.Lock()
_cl_manager = None        # kept alive across calls: holds authorization state
_cl_delegate = None
_cl_missing = False       # sticky: the framework itself isn't importable
_cl_delegate_class = None
# The delegate writes here. Only ever touched under _cl_lock, and only one
# query runs at a time, so a single module-level dict is enough.
_pending: dict = {}


def _delegate_class(objc):
    """Define the CLLocationManagerDelegate subclass once per process.

    Re-running the class body would register a duplicate ObjC class name and
    raise, so the result latches. Neither method returns a value: PyObjC
    type-checks against the selector's ObjC `void` return and raises inside
    the callback thread otherwise, which surfaces as an uncaught NSException
    that aborts the process rather than something we could catch here.
    """
    global _cl_delegate_class
    if _cl_delegate_class is not None:
        return _cl_delegate_class

    class _FridayLocationDelegate(objc.lookUpClass("NSObject")):
        def locationManager_didUpdateLocations_(self, manager, locations):
            loc = locations.lastObject() if locations else None
            if loc is not None:
                coord = loc.coordinate()
                _pending["lat"] = float(coord.latitude)
                _pending["lon"] = float(coord.longitude)
                _pending["accuracy_m"] = float(loc.horizontalAccuracy())
            _pending["done"] = True

        def locationManager_didFailWithError_(self, manager, error):
            _pending["error"] = str(error)
            _pending["done"] = True

    _cl_delegate_class = _FridayLocationDelegate
    return _cl_delegate_class


def _corelocation_fix():
    """A {lat, lon, accuracy_m} fix from Location Services, or None."""
    global _cl_manager, _cl_delegate, _cl_missing
    with _cl_lock:
        if _cl_missing:
            return None
        try:
            import CoreLocation
            import Foundation
            import objc
        except ImportError as e:
            logger.info(f"CoreLocation unavailable ({e}); using IP geolocation.")
            _cl_missing = True
            return None

        try:
            if not CoreLocation.CLLocationManager.locationServicesEnabled():
                logger.info("Location Services are off system-wide; "
                            "using IP geolocation.")
                return None

            if _cl_manager is None:
                _cl_delegate = _delegate_class(objc).alloc().init()
                _cl_manager = CoreLocation.CLLocationManager.alloc().init()
                _cl_manager.setDelegate_(_cl_delegate)
                _cl_manager.setDesiredAccuracy_(
                    CoreLocation.kCLLocationAccuracyHundredMeters)
                # No-op when the process has no usage-description key, which
                # is exactly the bare-interpreter case described up top.
                if hasattr(_cl_manager, "requestWhenInUseAuthorization"):
                    _cl_manager.requestWhenInUseAuthorization()

            _pending.clear()
            _cl_manager.startUpdatingLocation()
            try:
                # Delegate callbacks arrive on the run loop, so this thread
                # has to pump it. Bounded: an unanswered prompt must not wedge
                # a Telegram handler holding the semaphore.
                loop = Foundation.NSRunLoop.currentRunLoop()
                deadline = time.monotonic() + _CL_TIMEOUT_S
                while not _pending.get("done") and time.monotonic() < deadline:
                    loop.runMode_beforeDate_(
                        Foundation.NSDefaultRunLoopMode,
                        Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05))
            finally:
                _cl_manager.stopUpdatingLocation()

            if "lat" not in _pending:
                logger.info(
                    "CoreLocation returned no fix (%s); using IP geolocation. "
                    "If this is the packaged app, grant Friday location access "
                    "in System Settings → Privacy & Security → Location "
                    "Services.", _pending.get("error", "timed out"))
                return None
            return {"lat": _pending["lat"],
                    "lon": _pending["lon"],
                    "accuracy_m": _pending.get("accuracy_m")}
        except Exception as e:
            logger.warning(f"CoreLocation lookup failed ({e}); "
                           "using IP geolocation.")
            return None


def _reverse_geocode(lat: float, lon: float) -> str:
    """A human place name for a coordinate, or '' if it can't be resolved.

    CLGeocoder rather than a web service: the coordinate came from the local
    machine, and this keeps it there.
    """
    try:
        import CoreLocation
        import Foundation
    except ImportError:
        return ""
    try:
        done: dict = {}

        def _completion(placemarks, error):
            # Must return None — see the note in _delegate_class.
            if placemarks:
                done["placemark"] = placemarks[0]
            if error is not None:
                done["error"] = str(error)
            done["done"] = True

        loc = CoreLocation.CLLocation.alloc().initWithLatitude_longitude_(lat, lon)
        CoreLocation.CLGeocoder.alloc().init(
        ).reverseGeocodeLocation_completionHandler_(loc, _completion)

        loop = Foundation.NSRunLoop.currentRunLoop()
        deadline = time.monotonic() + _GEOCODE_TIMEOUT_S
        while not done.get("done") and time.monotonic() < deadline:
            loop.runMode_beforeDate_(
                Foundation.NSDefaultRunLoopMode,
                Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.05))

        pm = done.get("placemark")
        if pm is None:
            return ""
        parts = [pm.locality(), pm.administrativeArea()]
        return ", ".join(p for p in parts if p)
    except Exception as e:
        logger.debug(f"Reverse geocode failed: {e}")
        return ""


# ── IP geolocation fallback ──────────────────────────────────────────────────

def _parse_ipinfo(d: dict):
    # {"city": "...", "region": "...", "loc": "30.4213,-87.2169"}
    lat, _, lon = (d.get("loc") or "").partition(",")
    if not lon:
        return None
    return float(lat), float(lon), d.get("city"), d.get("region")


def _parse_ipwhois(d: dict):
    if not d.get("success", True) or d.get("latitude") is None:
        return None
    return float(d["latitude"]), float(d["longitude"]), d.get("city"), d.get("region")


# Tried in order. Both are keyless and HTTPS. Two of them because these
# services rate-limit by source IP without warning — ipapi.co, the obvious
# first choice, returned 429 on the very first request from this network and
# was dropped for it. A single provider means "where am I" fails outright the
# day that provider throttles.
_IP_PROVIDERS = (
    ("https://ipinfo.io/json", _parse_ipinfo),
    ("https://ipwho.is/", _parse_ipwhois),
)


def _ip_fix():
    """A city-level fix from the network's public IP, or None."""
    for url, parse in _IP_PROVIDERS:
        try:
            r = requests.get(url, timeout=8)
            r.raise_for_status()
            parsed = parse(r.json())
            if parsed is None:
                logger.warning(f"IP geolocation refused by {url}")
                continue
            lat, lon, city, region = parsed
            return {
                "lat": lat,
                "lon": lon,
                "accuracy_m": None,
                "place": ", ".join(p for p in (city, region) if p),
            }
        except Exception as e:
            logger.warning(f"IP geolocation via {url} failed: {e}")
    return None


# ── public API ───────────────────────────────────────────────────────────────

def fetch(max_age_s: float = _CACHE_TTL_S) -> dict | None:
    """Current machine location, or None if no backend could answer.

    Returns {source, lat, lon, accuracy_m, place}. `source` is "corelocation"
    (device-level, metres) or "ip" (network-level, city at best) — callers
    must not present the two as equally precise.

    Cached for `max_age_s`; an always-on Mac does not move, and a CoreLocation
    round trip can take seconds.
    """
    with _cache_lock:
        if _cache["fix"] and (time.monotonic() - _cache["at"]) < max_age_s:
            return _cache["fix"]

    fix = _corelocation_fix()
    if fix is not None:
        fix["source"] = "corelocation"
        fix["place"] = _reverse_geocode(fix["lat"], fix["lon"])
    else:
        fix = _ip_fix()
        if fix is not None:
            fix["source"] = "ip"

    if fix is None:
        return None

    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["fix"] = fix
    return fix


def describe() -> str:
    """One-line human summary of where this machine is, or '' on failure."""
    fix = fetch()
    if fix is None:
        return ""
    place = fix.get("place") or f"{fix['lat']:.4f}, {fix['lon']:.4f}"
    if fix["source"] == "corelocation":
        acc = fix.get("accuracy_m")
        precision = f" (±{acc:.0f} m)" if acc and acc > 0 else ""
        return f"{place}{precision}, from macOS Location Services."
    return f"{place}, approximated from the network connection."
