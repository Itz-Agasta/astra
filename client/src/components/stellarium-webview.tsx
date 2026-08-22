"use client";

import catalog from "@/data/bright-stars.json";
import { Button } from "@/components/ui/button";
import {
  type ObjectInfo,
  type SkyMarker,
  type StellariumStatus,
  type StellariumView,
  type Vec3,
  SOLAR_SYSTEM,
  equatorialBasis,
  formatDecDms,
  formatRaHms,
  objectMarker,
  parseVec3,
  raDecToVec,
  selectionTitle,
  slerp,
  stellariumGet,
  vecToAltAz,
  vecToRaDec,
} from "@/lib/stellarium";
import { useCallback, useEffect, useRef, useState } from "react";

type Star = [number, number, number, number];

const STARS = catalog.stars as Star[];

type LinkState = "checking" | "live" | "offline";

type Hud = {
  raDeg: number;
  decDeg: number;
  fov: number;
  altDeg: number;
  azDeg: number;
  target: string;
  utc: string;
  local: string;
  location: string;
};

const POLL_MS = 250;
const BODIES_MS = 2500;

function bvColor(bv: number): [number, number, number] {
  const t = Math.max(-0.4, Math.min(2.0, bv));
  if (t < 0) {
    const u = (t + 0.4) / 0.4;
    return [155 + 50 * u, 176 + 40 * u, 255];
  }
  if (t < 0.6) {
    const u = t / 0.6;
    return [205 + 50 * u, 216 + 28 * u, 255 - 20 * u];
  }
  if (t < 1.5) {
    const u = (t - 0.6) / 0.9;
    return [255, 244 - 40 * u, 234 - 90 * u];
  }
  const u = (t - 1.5) / 0.5;
  return [255, 204 - 30 * u, 144 - 50 * u];
}

function markerStyle(kind: SkyMarker["kind"]): { fill: string; r: number } {
  switch (kind) {
    case "sun":
      return { fill: "#f4d35e", r: 8 };
    case "moon":
      return { fill: "#e8e6e3", r: 6 };
    case "planet":
      return { fill: "#d4a574", r: 4.5 };
    default:
      return { fill: "#7dd3fc", r: 5 };
  }
}

function project(
  vec: Vec3,
  east: Vec3,
  north: Vec3,
  center: Vec3,
  fovY: number,
  width: number,
  height: number,
): { x: number; y: number; front: boolean } | null {
  const z = vec[0] * center[0] + vec[1] * center[1] + vec[2] * center[2];
  if (z <= 0.02) {
    return null;
  }
  const xCam = vec[0] * east[0] + vec[1] * east[1] + vec[2] * east[2];
  const yCam = vec[0] * north[0] + vec[1] * north[1] + vec[2] * north[2];
  const tanHalf = Math.tan(((fovY / 2) * Math.PI) / 180);
  const scale = height / 2 / tanHalf;
  const x = width / 2 + (xCam / z) * scale;
  const y = height / 2 - (yCam / z) * scale;
  const pad = 48;
  if (x < -pad || y < -pad || x > width + pad || y > height + pad) {
    return null;
  }
  return { x, y, front: true };
}

export function StellariumWebview() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const targetRef = useRef<Vec3>([1, 0, 0]);
  const drawnRef = useRef<Vec3>([1, 0, 0]);
  const fovTargetRef = useRef(60);
  const fovDrawnRef = useRef(60);
  const markersRef = useRef<SkyMarker[]>([]);
  const altAzRef = useRef<Vec3 | null>(null);
  const [link, setLink] = useState<LinkState>("checking");
  const [hud, setHud] = useState<Hud | null>(null);
  const [error, setError] = useState<string | null>(null);

  const pollOnce = useCallback(async () => {
    const [status, view] = await Promise.all([
      stellariumGet<StellariumStatus>("main/status"),
      stellariumGet<StellariumView>("main/view"),
    ]);
    const vec = parseVec3(view.j2000);
    if (!vec) {
      throw new Error("invalid view vector");
    }
    targetRef.current = vec;
    fovTargetRef.current = status.view.fov;
    altAzRef.current = parseVec3(view.altAz);

    const { raDeg, decDeg } = vecToRaDec(vec);
    const altAz = parseVec3(view.altAz);
    const horiz = altAz ? vecToAltAz(altAz) : { altDeg: 0, azDeg: 0 };

    let target = selectionTitle(status.selectioninfo);
    try {
      const info = await stellariumGet<ObjectInfo>("objects/info?format=json");
      const marker = objectMarker(info, "selection");
      if (marker) {
        target = marker.name;
        markersRef.current = [
          marker,
          ...markersRef.current.filter(
            (m: SkyMarker) => m.kind !== "selection" && m.name !== marker.name,
          ),
        ];
      }
    } catch {
      markersRef.current = markersRef.current.filter((m: SkyMarker) => m.kind !== "selection");
    }

    setHud({
      raDeg,
      decDeg,
      fov: status.view.fov,
      altDeg: horiz.altDeg,
      azDeg: horiz.azDeg,
      target,
      utc: status.time.utc,
      local: status.time.local,
      location: status.location.name,
    });
  }, []);

  const pollBodies = useCallback(async () => {
    const found: SkyMarker[] = [];
    await Promise.all(
      SOLAR_SYSTEM.map(async (body) => {
        try {
          const info = await stellariumGet<ObjectInfo>(
            `objects/info?name=${encodeURIComponent(body.name)}&format=json`,
          );
          const marker = objectMarker(info, body.kind);
          if (marker) {
            found.push(marker);
          }
        } catch {
          /* object not in current Stellarium catalogs */
        }
      }),
    );
    const selection = markersRef.current.filter((m: SkyMarker) => m.kind === "selection");
    markersRef.current = [
      ...selection,
      ...found.filter((m) => !selection.some((s: SkyMarker) => s.name === m.name)),
    ];
  }, []);

  useEffect(() => {
    let cancelled = false;
    let failures = 0;

    const tick = async () => {
      try {
        await pollOnce();
        if (cancelled) {
          return;
        }
        failures = 0;
        setLink("live");
        setError(null);
      } catch (err) {
        if (cancelled) {
          return;
        }
        failures += 1;
        if (failures >= 2) {
          setLink("offline");
          setError(err instanceof Error ? err.message : "stellarium unreachable");
        }
      }
    };

    void tick();
    const pollId = window.setInterval(() => void tick(), POLL_MS);
    const bodyId = window.setInterval(() => void pollBodies(), BODIES_MS);
    void pollBodies();

    return () => {
      cancelled = true;
      window.clearInterval(pollId);
      window.clearInterval(bodyId);
    };
  }, [pollBodies, pollOnce]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) {
      return;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      return;
    }

    let raf = 0;
    let last = performance.now();
    let running = true;

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const { width, height } = wrap.getBoundingClientRect();
      canvas.width = Math.max(1, Math.floor(width * dpr));
      canvas.height = Math.max(1, Math.floor(height * dpr));
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    const ro = new ResizeObserver(resize);
    ro.observe(wrap);
    resize();

    const draw = (now: number) => {
      if (!running) {
        return;
      }
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const follow = 1 - Math.exp(-dt / 0.12);
      drawnRef.current = slerp(drawnRef.current, targetRef.current, follow);
      fovDrawnRef.current += (fovTargetRef.current - fovDrawnRef.current) * follow;

      const { width, height } = wrap.getBoundingClientRect();
      const center = drawnRef.current;
      const { east, north } = equatorialBasis(center);
      const fovY = fovDrawnRef.current;
      const magLimit = fovY > 40 ? 5.2 : fovY > 20 ? 5.8 : 6.0;

      ctx.fillStyle = "#02040a";
      ctx.fillRect(0, 0, width, height);

      const altAz = altAzRef.current;
      if (altAz) {
        const { altDeg } = vecToAltAz(altAz);
        const horizonNdc = Math.tan(((altDeg - 0) * Math.PI) / 180) / Math.tan(((fovY / 2) * Math.PI) / 180);
        const horizonY = height / 2 + (horizonNdc * height) / 2;
        if (horizonY < height) {
          const y = Math.max(0, horizonY);
          const g = ctx.createLinearGradient(0, y, 0, height);
          g.addColorStop(0, "rgba(18, 22, 28, 0.0)");
          g.addColorStop(0.12, "rgba(22, 28, 24, 0.55)");
          g.addColorStop(1, "rgba(12, 14, 12, 0.92)");
          ctx.fillStyle = g;
          ctx.fillRect(0, y, width, height - y);
          ctx.strokeStyle = "rgba(80, 110, 70, 0.35)";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(0, y);
          ctx.lineTo(width, y);
          ctx.stroke();
        }
      }

      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      for (const [ra, dec, mag, bv] of STARS) {
        if (mag > magLimit) {
          continue;
        }
        const vec = raDecToVec(ra, dec);
        const p = project(vec, east, north, center, fovY, width, height);
        if (!p) {
          continue;
        }
        const [r, g, b] = bvColor(bv);
        const bright = Math.max(0.15, 10 ** (-0.22 * (mag + 1.2)));
        const radius = mag < 0.5 ? 2.8 : mag < 1.5 ? 2.1 : mag < 3 ? 1.5 : mag < 4.5 ? 1.05 : 0.7;
        if (mag < 2.2) {
          const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, radius * 6);
          glow.addColorStop(0, `rgba(${r},${g},${b},${0.55 * bright})`);
          glow.addColorStop(1, `rgba(${r},${g},${b},0)`);
          ctx.fillStyle = glow;
          ctx.beginPath();
          ctx.arc(p.x, p.y, radius * 6, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.fillStyle = `rgba(${r},${g},${b},${Math.min(1, 0.35 + bright)})`;
        ctx.beginPath();
        ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();

      ctx.strokeStyle = "rgba(140, 170, 210, 0.07)";
      ctx.lineWidth = 1;
      const { raDeg } = vecToRaDec(center);
      const ra0 = Math.round(raDeg / 15) * 15;
      for (let ra = ra0 - 30; ra <= ra0 + 30; ra += 15) {
        ctx.beginPath();
        let started = false;
        for (let dec = -80; dec <= 80; dec += 4) {
          const p = project(raDecToVec((ra + 360) % 360, dec), east, north, center, fovY, width, height);
          if (!p) {
            started = false;
            continue;
          }
          if (!started) {
            ctx.moveTo(p.x, p.y);
            started = true;
          } else {
            ctx.lineTo(p.x, p.y);
          }
        }
        ctx.stroke();
      }

      for (const marker of markersRef.current) {
        const p = project(raDecToVec(marker.raDeg, marker.decDeg), east, north, center, fovY, width, height);
        if (!p) {
          continue;
        }
        const style = markerStyle(marker.kind);
        ctx.fillStyle = style.fill;
        ctx.strokeStyle = "rgba(255,255,255,0.7)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(p.x, p.y, style.r, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
        ctx.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
        ctx.fillStyle = "rgba(248,250,252,0.9)";
        ctx.fillText(marker.name, p.x + style.r + 6, p.y + 4);
      }

      const cx = width / 2;
      const cy = height / 2;
      ctx.strokeStyle = "rgba(125, 211, 252, 0.55)";
      ctx.lineWidth = 1.25;
      const arm = 18;
      const gap = 7;
      ctx.beginPath();
      ctx.moveTo(cx - arm, cy);
      ctx.lineTo(cx - gap, cy);
      ctx.moveTo(cx + gap, cy);
      ctx.lineTo(cx + arm, cy);
      ctx.moveTo(cx, cy - arm);
      ctx.lineTo(cx, cy - gap);
      ctx.moveTo(cx, cy + gap);
      ctx.lineTo(cx, cy + arm);
      ctx.stroke();
      ctx.beginPath();
      ctx.arc(cx, cy, 26, 0, Math.PI * 2);
      ctx.stroke();

      raf = window.requestAnimationFrame(draw);
    };

    raf = window.requestAnimationFrame(draw);
    return () => {
      running = false;
      window.cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  return (
    <div className="relative min-h-0 h-full overflow-hidden bg-[#02040a] text-zinc-100">
      <div ref={wrapRef} className="absolute inset-0">
        <canvas ref={canvasRef} className="block h-full w-full" />
      </div>

      <div className="pointer-events-none absolute inset-x-0 top-0 z-10 flex items-start justify-between gap-4 p-3">
        <div className="rounded-md border border-white/10 bg-black/45 px-3 py-2 font-mono text-[11px] leading-relaxed backdrop-blur-sm">
          <div className="flex items-center gap-2 text-[10px] tracking-[0.18em] text-zinc-400 uppercase">
            <span
              className={`inline-block size-1.5 rounded-full ${
                link === "live"
                  ? "bg-emerald-400 shadow-[0_0_8px_#34d399]"
                  : link === "checking"
                    ? "bg-amber-400"
                    : "bg-red-500"
              }`}
            />
            {link === "live" ? "Live · Stellarium" : link === "checking" ? "Connecting" : "Offline"}
          </div>
          {hud ? (
            <dl className="mt-1.5 grid grid-cols-[4.5rem_1fr] gap-x-2 gap-y-0.5 text-zinc-200">
              <dt className="text-zinc-500">RA</dt>
              <dd>{formatRaHms(hud.raDeg)}</dd>
              <dt className="text-zinc-500">Dec</dt>
              <dd>{formatDecDms(hud.decDeg)}</dd>
              <dt className="text-zinc-500">FOV</dt>
              <dd>{hud.fov.toFixed(2)}°</dd>
              <dt className="text-zinc-500">Alt / Az</dt>
              <dd>
                {hud.altDeg.toFixed(1)}° / {hud.azDeg.toFixed(1)}°
              </dd>
            </dl>
          ) : (
            <p className="mt-1.5 text-zinc-500">Waiting for :8090</p>
          )}
        </div>

        <div className="rounded-md border border-white/10 bg-black/45 px-3 py-2 text-right font-mono text-[11px] leading-relaxed backdrop-blur-sm">
          <div className="text-[10px] tracking-[0.18em] text-zinc-400 uppercase">Target</div>
          <div className="mt-1 max-w-72 truncate text-zinc-100">{hud?.target || "—"}</div>
          <div className="mt-2 text-zinc-500">{hud?.location}</div>
          <div className="text-zinc-400">{hud?.utc.replace("T", " ").replace("Z", " UTC")}</div>
        </div>
      </div>

      {link === "offline" ? (
        <div className="absolute inset-0 z-20 flex items-center justify-center bg-black/55 p-6 backdrop-blur-[2px]">
          <div className="max-w-md rounded-lg border border-white/10 bg-zinc-950/90 p-5 text-sm">
            <h2 className="font-mono text-xs tracking-[0.2em] text-zinc-400 uppercase">Stellarium unreachable</h2>
            <p className="mt-2 text-zinc-300">
              The desktop app must be running with the Remote Control plugin enabled on port 8090.
              This view follows pointing, FOV, time, and selection from that instance — it does not
              replace the calibration loop.
            </p>
            {error ? <p className="mt-2 font-mono text-xs text-red-400">{error}</p> : null}
            <Button
              className="pointer-events-auto mt-4"
              variant="secondary"
              onClick={() => {
                setLink("checking");
                void pollOnce()
                  .then(() => setLink("live"))
                  .catch(() => setLink("offline"));
              }}
            >
              Retry
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
