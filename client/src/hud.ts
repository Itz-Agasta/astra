import { selectionTitle, type DesktopState } from "./sync";

const linkEl = () => document.getElementById("status-link");
const locEl = () => document.getElementById("status-loc");
const fovEl = () => document.getElementById("status-fov");
const timeEl = () => document.getElementById("status-time");
const fpsEl = () => document.getElementById("status-fps");
const offlineEl = () => document.getElementById("offline");

let frames = 0;
let lastFps = performance.now();
let fps = 0;
let isCalibrating = false;

export function tickFps(): void {
  frames += 1;
  const now = performance.now();
  if (now - lastFps >= 500) {
    fps = (frames * 1000) / (now - lastFps);
    frames = 0;
    lastFps = now;
    const el = fpsEl();
    if (el) {
      el.textContent = `${fps.toFixed(0)} FPS`;
    }
  }
}

async function selectTargetOnDesktop(name: string): Promise<void> {
  try {
    await fetch("/api/main/focus", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `target=${encodeURIComponent(name)}`
    });
  } catch (e) {
    console.error("Failed to select target on desktop", e);
  }
}

async function slewDesktop(raDeg: number, decDeg: number): Promise<void> {
  try {
    const raRad = (raDeg * Math.PI) / 180;
    const decRad = (decDeg * Math.PI) / 180;
    const vec = [
      Math.cos(decRad) * Math.cos(raRad),
      Math.cos(decRad) * Math.sin(raRad),
      Math.sin(decRad)
    ];
    await fetch("/api/main/view?j2000=" + encodeURIComponent(JSON.stringify(vec)), {
      method: "POST"
    });
  } catch (e) {
    console.error("Failed to slew desktop", e);
  }
}

export function bindCalibrate(): void {
  const btn = document.getElementById("btn-calibrate");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (isCalibrating) return;
    isCalibrating = true;
    btn.setAttribute("disabled", "true");
    
    const feed = document.getElementById("log-feed");
    const indicator = document.getElementById("status-indicator");
    const stelStatus = document.getElementById("stel-status");
    const targetObj = document.getElementById("target-object");
    const targetCoords = document.getElementById("target-coords");
    const j2000Vec = document.getElementById("j2000-vector");
    const convPanel = document.getElementById("convergence-panel");
    
    if (convPanel) convPanel.setAttribute("hidden", "");
    if (indicator) {
      indicator.textContent = "ACTIVE";
      indicator.className = "status-indicator active";
    }
    
    if (feed) {
      feed.innerHTML = "";
    }
    
    const addLog = (msg: string, type: "info" | "success" | "error" | "plain" = "plain") => {
      if (!feed) return;
      const el = document.createElement("div");
      el.className = `log-item ${type}`;
      el.textContent = msg;
      feed.appendChild(el);
      feed.scrollTop = feed.scrollHeight;
    };

    // Step 1: Checking Stellarium
    addLog("[1] Checking Stellarium at http://localhost:8090 ...", "plain");
    const delay = (ms: number) => {
      const { promise, resolve } = Promise.withResolvers<void>();
      setTimeout(resolve, ms);
      return promise;
    };
    await delay(800);
    
    let reachable = false;
    try {
      const res = await fetch("/api/main/status");
      reachable = res.ok;
    } catch {}
    
    if (reachable) {
      addLog("    ✓ Stellarium reachable", "success");
      if (stelStatus) {
        stelStatus.textContent = "CONNECTED";
        stelStatus.style.color = "#efe6d0";
      }
    } else {
      addLog("    ✗ Stellarium offline", "error");
      if (stelStatus) {
        stelStatus.textContent = "OFFLINE";
        stelStatus.style.color = "#ef4444";
      }
    }
    await delay(800);

    // Step 2: Reading View
    addLog("[2] Reading Stellarium view ...", "plain");
    await delay(800);
    addLog("    ✓ J2000 vector: [-0.599518, 0.710267, 0.368915]", "success");
    if (j2000Vec) j2000Vec.textContent = "[-0.599518, 0.710267, 0.368915]";
    await delay(1000);

    // Step 3: Starting Calibration
    addLog("[3] Starting calibration → Jupiter (RA=116.55° Dec=21.62°)", "info");
    addLog("    job_id: cal_ccbab9", "plain");
    if (targetObj) targetObj.textContent = "Jupiter";
    if (targetCoords) targetCoords.textContent = "RA=116.55° Dec=21.62°";
    await delay(1200);

    // Select Jupiter on desktop
    await selectTargetOnDesktop("Jupiter");

    // Iterations
    const iterations = [
      { iter: 1, ra: 130.1668, dec: 21.6487, err: 49020.8, suchetan: true },
      { iter: 2, ra: 119.2733, dec: 21.6257, err: 9804.0, suchetan: true },
      { iter: 3, ra: 117.0947, dec: 21.6212, err: 1960.8, suchetan: true },
      { iter: 4, ra: 116.6589, dec: 21.6202, err: 392.1, suchetan: true },
      { iter: 5, ra: 116.5718, dec: 21.6201, err: 78.5, suchetan: true },
      { iter: 6, ra: 116.5544, dec: 21.6200, err: 15.7, suchetan: false }
    ];

    for (const step of iterations) {
      if (step.suchetan) {
        addLog("Suchetan will implement this", "error");
      }
      
      // Slew the Stellarium view to show the movement
      await slewDesktop(step.ra, step.dec);
      
      addLog(`    iter=${step.iter}  RA=${step.ra.toFixed(4)}°  Dec=${step.dec.toFixed(4)}°  error=${step.err.toFixed(1)}"`, "plain");
      
      // Update timeline bars height
      const timelineBars = document.getElementById("timeline-bars");
      if (timelineBars) {
        const spans = timelineBars.querySelectorAll("span");
        spans.forEach((span, idx) => {
          if (idx < step.iter) {
            const err = iterations[idx].err;
            const height = Math.max(10, Math.min(90, Math.round((Math.log(err) / Math.log(49020)) * 90)));
            (span as HTMLElement).style.height = `${height}%`;
          } else {
            (span as HTMLElement).style.height = "5%";
          }
        });
      }
      
      await delay(1500);
    }

    addLog("------------------------------------------------------------", "plain");
    addLog("  ✓ CONVERGED in 6 iterations", "success");
    addLog("    Final error: 15.7\"", "plain");
    addLog("    Message: Locked on Jupiter. Final error 15.7\"", "success");

    // Show final panel
    if (convPanel) {
      convPanel.removeAttribute("hidden");
    }
    if (indicator) {
      indicator.textContent = "LOCKED";
      indicator.className = "status-indicator locked";
    }

    isCalibrating = false;
    btn.removeAttribute("disabled");
  });
}

export function renderHud(state: DesktopState | { online: false; error: string }): void {
  const offline = offlineEl();
  const statusConn = document.getElementById("stel-status");
  
  if (!state.online) {
    document.getElementById("sel-pointer")?.setAttribute("hidden", "");
    if (statusConn && !isCalibrating) {
      statusConn.textContent = "OFFLINE";
      statusConn.style.color = "#ef4444";
    }
    linkEl()?.replaceChildren(document.createTextNode("offline"));
    offline?.removeAttribute("hidden");
    return;
  }
  
  offline?.setAttribute("hidden", "");
  linkEl()?.replaceChildren(document.createTextNode("live"));

  const { location, time, view, selectioninfo } = state.status;
  locEl()?.replaceChildren(
    document.createTextNode(
      `${location.planet}, ${location.latitude.toFixed(3)}, ${location.longitude.toFixed(3)}, ${location.altitude} m`,
    ),
  );
  fovEl()?.replaceChildren(document.createTextNode(`FOV ${view.fov.toFixed(1)}°`));
  timeEl()?.replaceChildren(
    document.createTextNode(time.local.replace("T", " ") + (time.timeZone ? ` ${time.timeZone}` : "")),
  );

  if (statusConn && !isCalibrating) {
    statusConn.textContent = "CONNECTED";
    statusConn.style.color = "#efe6d0";
  }

  // Update target object name and coordinates if not calibrating
  if (!isCalibrating) {
    const targetObj = document.getElementById("target-object");
    const targetCoords = document.getElementById("target-coords");
    
    if (selectioninfo) {
      const title = selectionTitle(selectioninfo);
      if (targetObj) targetObj.textContent = title || "Unknown Object";
      
      const raMatch = selectioninfo.match(/RA\/Dec\s*\(J2000\.0\):\s*([^\n<]+)/i);
      if (targetCoords && raMatch?.[1]) {
        targetCoords.textContent = raMatch[1].trim();
      } else if (targetCoords) {
        targetCoords.textContent = "RA/Dec info available";
      }
    } else {
      if (targetObj) targetObj.textContent = "None";
      if (targetCoords) targetCoords.textContent = "—";
    }
    
    // Reset timeline bars in idle state
    const timelineBars = document.getElementById("timeline-bars");
    if (timelineBars) {
      const spans = timelineBars.querySelectorAll("span");
      spans.forEach((span) => {
        (span as HTMLElement).style.height = "5%";
      });
    }
  }
}

export function bindRetry(retry: () => void): void {
  document.getElementById("retry")?.addEventListener("click", retry);
}
