import { bindCalibrate, bindRetry, renderHud, tickFps } from "./hud";
import { initEngine } from "./stel-engine";
import { startSync } from "./sync";
import "./style.css";

const canvas = document.getElementById("stel-canvas");
if (!(canvas instanceof HTMLCanvasElement)) {
  throw new Error("missing #stel-canvas");
}
bindCalibrate();

const engineReady = initEngine(canvas).catch((err: unknown) => {
  console.error(err);
  renderHud({ online: false, error: err instanceof Error ? err.message : "engine failed" });
  return null;
});

let stop: (() => void) | null = null;

function connect(): void {
  stop?.();
  stop = startSync((state) => {
    renderHud(state);
  });
}

void engineReady.then((engine) => {
  if (engine) {
    connect();
  }
});
bindRetry(() => {
  void engineReady.then((engine) => {
    if (engine) {
      connect();
    }
  });
});

const loop = () => {
  tickFps();
  window.requestAnimationFrame(loop);
};
window.requestAnimationFrame(loop);
