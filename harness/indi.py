"""
indi.py -- a minimal INDI protocol client.

INDI (Instrument-Neutral Distributed Interface) is what real observatory
hardware speaks. An `indiserver` process loads one driver per device and
relays XML over TCP 7624; clients discover properties by asking, then read
and write them by name. Every mount driver implements the same standard
properties, which is why the harness does not care whether it is talking to
`indi_simulator_telescope` or a telescope on a mountain.

We speak the wire protocol directly rather than take a dependency:
`pyindi-client` is SWIG and needs libindi headers to build, and
`indipyclient` is an event-driven framework whose long-running `asyncrun()`
loop fits badly with the request/response style used everywhere else here.
We need five properties, and this is smaller than the adapter would be.

Reference: https://docs.indilib.org/protocol/
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger(__name__)

INDI_PORT = 7624
INDI_VERSION = "1.7"

# Standard properties. Any conforming mount driver defines these.
CONNECTION = "CONNECTION"
ON_COORD_SET = "ON_COORD_SET"
EQUATORIAL_EOD_COORD = "EQUATORIAL_EOD_COORD"  # what the mount believes, JNow
EQUATORIAL_PE = "EQUATORIAL_PE"  # simulator ground truth, JNow
ABORT_MOTION = "TELESCOPE_ABORT_MOTION"
MOUNT_MODEL = "MOUNT_MODEL"  # simulator pointing-error injection, arcminutes

_VECTOR_TAGS = ("defNumberVector", "setNumberVector", "defSwitchVector", "setSwitchVector")


class IndiError(RuntimeError):
    pass


class IndiClient:
    """One connection to an indiserver, tracking one device's properties."""

    def __init__(self, host: str, port: int = INDI_PORT, device: str = "Telescope Simulator"):
        self.host = host
        self.port = port
        self.device = device
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        # name -> {"state": str, "elements": {element: value}}
        self._props: dict[str, dict] = {}
        self._updated = asyncio.Event()

    #  Connection

    async def connect(self, timeout: float = 10.0) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=timeout
        )
        self._task = asyncio.create_task(self._read_loop())
        await self._send(f'<getProperties version="{INDI_VERSION}"/>')
        log.info(f"INDI connected to {self.host}:{self.port}")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done()

    #  Reading

    async def _read_loop(self) -> None:
        try:
            await self._read_stream()
        except asyncio.CancelledError:
            raise
        except Exception:
            # A dead reader means every wait below silently times out instead
            # of reporting why, so make the real cause visible.
            log.exception("INDI reader stopped")
            raise

    async def _read_stream(self) -> None:
        """Parse the inbound stream into the property cache.

        The stream is a *sequence* of top-level elements rather than one
        document, so it is fed to a pull parser primed with a synthetic root.
        Root children are cleared as they are consumed -- a mount publishing
        its position four times a second would otherwise grow the tree without
        bound for as long as the harness runs.
        """
        assert self._reader is not None
        parser = ET.XMLPullParser(events=("start", "end"))
        parser.feed(b"<indi>")
        root: ET.Element | None = None
        depth = 0

        while True:
            chunk = await self._reader.read(65536)
            if not chunk:
                log.warning("INDI connection closed by server")
                return
            parser.feed(chunk)
            # read_events() is typed for every event kind, including the
            # namespace ones that yield a 2-tuple of strings. We asked for
            # start/end only, so an Element is what actually arrives.
            events = cast("Iterator[tuple[str, ET.Element]]", parser.read_events())
            for event, elem in events:
                if event == "start":
                    depth += 1
                    if root is None:
                        root = elem
                    continue
                depth -= 1
                if depth != 1:  # not a direct child of the synthetic root
                    continue
                self._handle(elem)
                if root is not None:
                    root.clear()

    def _handle(self, elem: ET.Element) -> None:
        if elem.get("device") != self.device or elem.tag not in _VECTOR_TAGS:
            return
        name = elem.get("name")
        if not name:
            return
        prop = self._props.setdefault(name, {"state": "Idle", "elements": {}})
        if state := elem.get("state"):
            prop["state"] = state
        for child in elem:
            child_name = child.get("name")
            if child_name:
                prop["elements"][child_name] = (child.text or "").strip()
        self._updated.set()

    #  Writing

    async def _send(self, xml: str) -> None:
        if self._writer is None:
            raise IndiError("Not connected")
        self._writer.write(xml.encode())
        await self._writer.drain()

    async def set_switch(self, name: str, values: dict[str, bool]) -> None:
        members = "".join(
            f'<oneSwitch name="{k}">{"On" if v else "Off"}</oneSwitch>' for k, v in values.items()
        )
        await self._send(
            f'<newSwitchVector device="{self.device}" name="{name}">{members}</newSwitchVector>'
        )
        self._mark_busy(name)

    async def set_number(self, name: str, values: dict[str, float]) -> None:
        members = "".join(f'<oneNumber name="{k}">{v}</oneNumber>' for k, v in values.items())
        await self._send(
            f'<newNumberVector device="{self.device}" name="{name}">{members}</newNumberVector>'
        )
        self._mark_busy(name)

    def _mark_busy(self, name: str) -> None:
        """Per the spec, the client sets its own copy Busy and lets the device
        clear it. Without this, a wait would return instantly on the stale Ok
        left over from the previous move."""
        self._props.setdefault(name, {"state": "Idle", "elements": {}})["state"] = "Busy"

    #  Queries

    def state(self, name: str) -> str | None:
        prop = self._props.get(name)
        return prop["state"] if prop else None

    def number(self, name: str, element: str) -> float | None:
        raw = self._props.get(name, {}).get("elements", {}).get(element)
        try:
            return float(raw)  # type: ignore[arg-type]
        except Exception:
            return None

    def switch(self, name: str, element: str) -> bool | None:
        raw = self._props.get(name, {}).get("elements", {}).get(element)
        return None if raw is None else raw == "On"

    def has(self, name: str) -> bool:
        return name in self._props

    def property_names(self) -> list[str]:
        return sorted(self._props)

    async def wait_defined(self, name: str, timeout: float = 10.0) -> None:
        """Block until the driver has defined a property.

        Drivers publish their properties asynchronously after `getProperties`,
        and a mount only defines its coordinate properties once connected, so
        anything touching them has to wait first.
        """
        await self._wait(lambda: name in self._props, timeout, f"property '{name}' never appeared")

    async def wait_state(self, name: str, target: str = "Ok", timeout: float = 120.0) -> None:
        """Block until a property reaches a state -- how a slew reports done.

        A mount sets the coordinate vector Busy while moving and Ok on
        arrival. Alert means it gave up.
        """

        def done() -> bool:
            state = self.state(name)
            if state == "Alert":
                raise IndiError(f"{self.device}.{name} went to Alert")
            return state == target

        await self._wait(done, timeout, f"{name} did not reach {target} within {timeout}s")

    async def _wait(self, predicate, timeout: float, message: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if predicate():
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise IndiError(message)
            self._updated.clear()
            try:
                await asyncio.wait_for(self._updated.wait(), timeout=min(remaining, 1.0))
            except TimeoutError:
                pass  # re-check the predicate; the driver may be quiet


#  Coordinate frames
#
# INDI works in JNow (mean equinox of date) and expresses RA in *hours*.
# Everything else in this harness is J2000 degrees -- tetra3's catalog frame,
# and the resolver's output. Precession between the two is about 22 arcminutes
# and growing. Getting this wrong does not raise: the loop simply never
# converges, and it looks like a mount fault.


def j2000_to_jnow(ra_deg: float, dec_deg: float) -> tuple[float, float]:
    """J2000 degrees -> JNow (RA hours, Dec degrees), ready for INDI."""
    import astropy.units as u
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time

    now = Time.now()
    jnow = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame="icrs").transform_to(
        FK5(equinox=now)
    )
    return (float(jnow.ra.deg) % 360.0) / 15.0, float(jnow.dec.deg)


def jnow_to_j2000(ra_hours: float, dec_deg: float) -> tuple[float, float]:
    """JNow (RA hours, Dec degrees) as reported by INDI -> J2000 degrees."""
    import astropy.units as u
    from astropy.coordinates import FK5, SkyCoord
    from astropy.time import Time

    now = Time.now()
    j2000 = SkyCoord(
        ra=ra_hours * 15.0 * u.deg, dec=dec_deg * u.deg, frame=FK5(equinox=now)
    ).transform_to("icrs")
    return float(j2000.ra.deg) % 360.0, float(j2000.dec.deg)
