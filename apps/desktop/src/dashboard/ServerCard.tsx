import { useEffect, useState } from "react";
import { version } from "../../package.json";
import { api, gb, System } from "./api";
import { Alert, CopyButton, Progress } from "./ui";

// Mirrors server() in host/app/engine_server.py.
export type MemoryGuard = "safe" | "balanced" | "aggressive" | "custom";
export type EngineServer = {
  status: "running" | "starting" | "stopped";
  address: string;
  up_since: number | null;
  has_model: boolean;
  settings: { idle_unload_minutes: number; idle_unload_by_model: Record<string, number>; memory_guard: MemoryGuard; reserve_vram_gb: number };
  restart_needed: boolean;
  versions: { comfyui: string | null; python: string | null; torch: string | null; custom_nodes: string[] } | null;
};

export const restartEngine = () => api<EngineServer>("/api/v1/engine/restart", { method: "POST" });

const STATUS = { running: "Running", starting: "Starting", stopped: "Stopped" } as const;
const DISMISSED = "glint.first-run-dismissed";

function uptime(since: number) {
  const m = Math.max(0, Math.floor((Date.now() / 1000 - since) / 60));
  return m < 60 ? `${m} min` : m < 1440 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 1440)} d ${Math.floor((m % 1440) / 60)} h`;
}

/** The engine server at a glance, at the top of the Overview: state, address, memory, restart and unload. */
export default function ServerCard({ system }: { system: System | null }) {
  const [server, setServer] = useState<EngineServer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const load = () => api<EngineServer>("/api/v1/engine/server").then(setServer, (e) => setError(e.message));
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  const act = (call: () => Promise<unknown>) => {
    setBusy(true);
    call()
      .then(() => api<EngineServer>("/api/v1/engine/server").then(setServer))
      .then(() => setError(null), (e) => setError(e.message))
      .finally(() => setBusy(false));
  };

  const device = system?.engine.devices[0];
  const used = device && device.vram_total ? device.vram_total - device.vram_free : null;
  const model = system?.engine.model;
  const v = server?.versions;
  const status = server?.status ?? "starting";

  return (
    <>
      {server && !server.has_model && <FirstRun />}
      <section className="card" aria-labelledby="server-title">
        <div className="card-head">
          <div>
            <h2 id="server-title">Server</h2>
            <span className="cap">
              Glint Studio v{version}
              {server?.up_since && status === "running" && ` · up ${uptime(server.up_since)}`}
            </span>
          </div>
          <span className={`badge ${status === "running" ? "now" : status === "stopped" ? "bad" : ""}`} role="status">
            {STATUS[status]}
          </span>
        </div>

        {error && <Alert>{error}</Alert>}
        {server?.restart_needed && <Alert info>Memory settings changed. Restart the server to apply them.</Alert>}

        {server && (
          <div className="actions">
            <code>{server.address}</code>
            <CopyButton text={server.address} label="Copy the server address" />
          </div>
        )}

        {used != null && device && (
          <div>
            <p className="small muted">
              Memory: {gb(used)} of {gb(device.vram_total)} in use
            </p>
            <Progress value={used / device.vram_total} label="Memory in use" />
          </div>
        )}

        {v && (
          <p className="cap">
            ComfyUI {v.comfyui ?? "–"} · Python {v.python ?? "–"} · PyTorch {v.torch ?? "–"}
            {v.custom_nodes.length > 0 && ` · Custom nodes: ${v.custom_nodes.join(", ")}`}
          </p>
        )}

        <div className="actions">
          <button className="small" disabled={busy || status === "starting"} onClick={() => act(restartEngine)}>
            Restart server
          </button>
          <button
            className="small"
            disabled={busy || !system?.engine.up || !model || model.status === "idle"}
            onClick={() => act(() => api("/api/v1/engine/unload", { method: "POST" }))}
          >
            Unload model
          </button>
        </div>
      </section>
    </>
  );
}

const read = () => {
  try {
    return localStorage.getItem(DISMISSED) === "1";
  } catch {
    return false;
  }
};

/** Shown until a model is installed or the banner is dismissed. */
function FirstRun() {
  const [hidden, setHidden] = useState(read);
  if (hidden) return null;
  const dismiss = () => {
    setHidden(true);
    try {
      localStorage.setItem(DISMISSED, "1");
    } catch {
      /* hidden until the page reloads */
    }
  };
  return (
    <Alert info>
      <strong>New here? You don't have a model yet.</strong>
      <div className="actions">
        <a className="button small" href="#models/discover">
          See recommended models
        </a>
        <button className="small ghost" onClick={dismiss}>
          Not now
        </button>
      </div>
    </Alert>
  );
}
