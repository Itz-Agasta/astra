from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone

import serial  # pyserial

from .config import cfg

log = logging.getLogger(__name__)

_ser: serial.Serial | None = None
_lock = asyncio.Lock()  # serializes access to the single shared serial port


async def connect(retries: int = 5) -> None:
    """Open the persistent USB-serial connection to the ESP32. Call once at startup.

    Retries with cfg.esp32.reconnect_delay_s between attempts. The ESP32 often
    hasn't finished enumerating as a USB serial device yet if it was plugged
    in / powered on around the same time the harness started -- opening the
    port on the first try is not reliable, so a single attempt is not enough.
    Raises the last error if every attempt fails, so the caller's try/except
    is reporting a real, exhausted failure rather than one bad first guess.
    """
    global _ser
    if _ser is not None and _ser.is_open:
        return

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            _ser = await asyncio.to_thread(
                serial.Serial,
                cfg.esp32.port,
                cfg.esp32.baud_rate,
                timeout=cfg.esp32.timeout_s,
            )
            await asyncio.sleep(2.0)  # ESP32 resets on port open -- give firmware time to boot
            log.info(
                f"ESP32 connected on {cfg.esp32.port} @ {cfg.esp32.baud_rate} baud "
                f"(attempt {attempt}/{retries})"
            )
            return
        except serial.SerialException as exc:
            last_exc = exc
            _ser = None
            log.warning(
                f"ESP32 connect attempt {attempt}/{retries} failed on {cfg.esp32.port}: {exc}"
            )
            if attempt < retries:
                await asyncio.sleep(cfg.esp32.reconnect_delay_s)

    raise RuntimeError(
        f"Could not open ESP32 serial port {cfg.esp32.port} after {retries} attempts"
    ) from last_exc


async def disconnect() -> None:
    """Close the serial connection. Call on shutdown."""
    global _ser
    if _ser is not None and _ser.is_open:
        await asyncio.to_thread(_ser.close)
        log.info("ESP32 disconnected")
    _ser = None


# --- RA/Dec -> Alt/Az (pure stdlib, no network calls) -----------------------

def _julian_date(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    y, m, d = dt.year, dt.month, dt.day
    ut_hours = dt.hour + dt.minute / 60 + dt.second / 3600
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + ut_hours / 24 + b - 1524.5


def _gmst_deg(jd: float) -> float:
    t = (jd - 2451545.0) / 36525.0
    gmst = (
        280.46061837
        + 360.98564736629 * (jd - 2451545.0)
        + 0.000387933 * t**2
        - (t**3) / 38710000.0
    )
    return gmst % 360.0


def ra_dec_to_alt_az(
    ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float, dt: datetime | None = None
) -> tuple[float, float]:
    """
    Approximate equatorial -> horizontal conversion. Good enough to point a
    mock mount roughly the right direction, not precision astrometry.
    Returns (az_deg, alt_deg).
    """
    dt = dt or datetime.now(timezone.utc)
    jd = _julian_date(dt)
    lst = (_gmst_deg(jd) + lon_deg) % 360.0

    ha_deg = (lst - ra_deg) % 360.0
    if ha_deg > 180.0:
        ha_deg -= 360.0

    ha, dec, lat = math.radians(ha_deg), math.radians(dec_deg), math.radians(lat_deg)

    sin_alt = math.sin(dec) * math.sin(lat) + math.cos(dec) * math.cos(lat) * math.cos(ha)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))

    cos_az = (math.sin(dec) - math.sin(alt) * math.sin(lat)) / (math.cos(alt) * math.cos(lat) + 1e-12)
    az = math.acos(max(-1.0, min(1.0, cos_az)))
    if math.sin(ha) > 0:
        az = 2 * math.pi - az

    return math.degrees(az), math.degrees(alt)


def _linear_map(value: float, in_min: float, in_max: float, out_min: float, out_max: float) -> float:
    value = max(in_min, min(in_max, value))
    return out_min + (value - in_min) * (out_max - out_min) / (in_max - in_min)


def ra_dec_to_servo_angles(ra_deg: float, dec_deg: float, dt: datetime | None = None) -> tuple[float, float]:
    """
    Convert equatorial (RA, Dec) to servo-ready angles for the mock mount's
    two pan/tilt axes. Returns (servo_az_deg, servo_alt_deg), clamped to 0-180.
    """
    az_deg, alt_deg = ra_dec_to_alt_az(
        ra_deg, dec_deg, cfg.observer.latitude, cfg.observer.longitude, dt
    )

    if cfg.esp32.invert_alt:
        alt_deg = -alt_deg
    alt_deg += cfg.esp32.alt_offset_deg

    az_deg = (az_deg + cfg.esp32.az_offset_deg) % 360.0
    if cfg.esp32.invert_az:
        az_deg = 360.0 - az_deg

    servo_alt = _linear_map(alt_deg, 0.0, 90.0, 0.0, 180.0)
    servo_az = max(0.0, min(180.0, az_deg / 2.0))

    return servo_az, servo_alt


# --- Serial command protocol -------------------------------------------------

async def _send_command(payload: dict) -> dict:
    """Send one newline-delimited JSON command, block until one JSON response line comes back."""
    if _ser is None or not _ser.is_open:
        raise RuntimeError("ESP32 serial connection not open -- call esp32.connect() first")

    line = json.dumps(payload) + "\n"

    async with _lock:
        try:
            await asyncio.to_thread(_ser.write, line.encode())
            raw = await asyncio.to_thread(_ser.readline)
        except serial.SerialException as e:
            log.error(f"Serial I/O error talking to ESP32: {e}")
            raise

    if not raw:
        raise TimeoutError(f"No response from ESP32 within {cfg.esp32.timeout_s}s")

    try:
        return json.loads(raw.decode().strip())
    except json.JSONDecodeError:
        raise RuntimeError(f"Malformed response from ESP32: {raw!r}")


async def signal_slew(ra_deg: float, dec_deg: float) -> None:
    az_angle, alt_angle = ra_dec_to_servo_angles(ra_deg, dec_deg)

    data = await _send_command({"cmd": "slew", "az_deg": az_angle, "alt_deg": alt_angle})
    if data.get("status") != "ok":
        raise RuntimeError(f"ESP32 slew failed: {data.get('message', 'unknown error')}")

    log.info(
        f"ESP32 slew acknowledged → RA={ra_deg:.4f}° Dec={dec_deg:.4f}° "
        f"(servo az={az_angle:.1f}° alt={alt_angle:.1f}°)"
    )


async def signal_halt() -> None:
    data = await _send_command({"cmd": "halt"})
    if data.get("status") != "ok":
        log.error(f"ESP32 halt failed: {data.get('message', 'unknown error')}")