"""
Manual smoke-test for the motor/esp32 path while the rest of the engine
(resolver/capture/solver/main) is still unfinished.

Run from the repo root:
    uv run python -m engine.test_motor
"""

from __future__ import annotations

import asyncio
import logging

from . import esp32, motor
from .config import cfg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("test_motor")


def print_conversion(name: str, ra_deg: float, dec_deg: float) -> None:
    az_deg, alt_deg = esp32.ra_dec_to_alt_az(
        ra_deg, dec_deg, cfg.observer.latitude, cfg.observer.longitude
    )
    servo_az, servo_alt = esp32.ra_dec_to_servo_angles(ra_deg, dec_deg)
    print(
        f"  {name}: RA={ra_deg:.2f}° Dec={dec_deg:.2f}°  ->  "
        f"Alt/Az: az={az_deg:.1f}° alt={alt_deg:.1f}°  ->  "
        f"servo: az={servo_az:.1f}° alt={servo_alt:.1f}°"
    )


async def main() -> None:
    print("=== Capybara motor/esp32 smoke test ===")
    print(f"Observer: lat={cfg.observer.latitude} lon={cfg.observer.longitude}")
    print(f"ESP32 port: {cfg.esp32.port} @ {cfg.esp32.baud_rate} baud")
    print(f"Simulation mode: {cfg.simulation.enabled}\n")

    # --- Part 1: pure conversion math, no hardware needed ---
    print("-- Conversion sanity check --")
    for name, ra, dec in [
        ("Polaris", 37.95, 89.26),
        ("Vega", 279.23, 38.78),
        ("Betelgeuse", 88.79, 7.41),
    ]:
        print_conversion(name, ra, dec)
    print()

    # --- Part 2: real serial round-trip to the ESP32 ---
    answer = (
        input("Connect to the ESP32 over serial and send real commands? [y/N] ").strip().lower()
    )
    if answer != "y":
        print("Skipping hardware test.")
        return

    try:
        await esp32.connect()
    except Exception as e:
        log.error(f"Failed to connect: {e}")
        return

    print(
        "\nCommands:\n"
        "  <ra_deg> <dec_deg>   -- signal ESP32 directly (esp32.signal_slew, skips Stellarium)\n"
        "  full <ra_deg> <dec_deg>  -- full motor.slew() path (also calls Stellarium if sim mode)\n"
        "  halt                 -- motor.halt()\n"
        "  quit                 -- disconnect and exit\n"
    )

    try:
        while True:
            raw = input("> ").strip()
            if not raw:
                continue
            if raw == "quit":
                break
            if raw == "halt":
                await motor.halt()
                continue

            parts = raw.split()
            full = parts[0] == "full"
            if full:
                parts = parts[1:]

            if len(parts) != 2:
                print("  usage: <ra_deg> <dec_deg>  or  full <ra_deg> <dec_deg>")
                continue

            try:
                ra_deg, dec_deg = float(parts[0]), float(parts[1])
            except ValueError:
                print("  ra_deg and dec_deg must be numbers")
                continue

            try:
                if full:
                    await motor.slew(ra_deg, dec_deg)
                else:
                    await esp32.signal_slew(ra_deg, dec_deg)
                print("  ok")
            except Exception as e:
                log.error(f"failed: {e}")
    finally:
        await esp32.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
