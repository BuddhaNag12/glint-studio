import { useEffect, useState } from "react";
import { Channel, invoke } from "@tauri-apps/api/core";
import type { Engine } from "./Onboarding";
import { Alert, Icon } from "../dashboard/ui";
import { ProviderKeys } from "./Providers";

// Mirrors Progress and HostInfo in src-tauri/src/runtime.rs; "start" is the start_host call.
type StepId = "uv" | "python" | "venv" | "host" | "torch" | "comfyui" | "start";
type Progress = { step: Exclude<StepId, "start">; done: boolean; detail: string | null };
export type HostInfo = { url: string; api_key: string };
export type Installed = { host: HostInfo; models: number };
type StepState = "pending" | "running" | "done" | "failed";

const CORE: [StepId, string][] = [
  ["uv", "Get the package manager (uv)"],
  ["python", "Install Python 3.12"],
  ["venv", "Create Glint Studio's own environment"],
  ["host", "Install the Glint Studio host"],
];
const ENGINE: [StepId, string][] = [
  ["torch", "Install PyTorch for your GPU"],
  ["comfyui", "Install ComfyUI (the image engine)"],
];
// The steps the user sees: each groups the installer's progress rows.
const groupsFor = (engine: Engine): [string, string, [StepId, string][]][] => [
  ["Glint Studio basics", "Python and the host, in a folder of their own", CORE],
  ...(engine === "local" ? [["Image engine", "PyTorch and ComfyUI, sized for your GPU", ENGINE] as [string, string, [StepId, string][]]] : []),
  ["Start", engine === "local" ? "ComfyUI and the host" : "The host", [["start", engine === "local" ? "Start ComfyUI and the host" : "Start the host"]]],
];
const allPending = () =>
  Object.fromEntries([...CORE, ...ENGINE, ["start"]].map(([id]) => [id, "pending"])) as Record<StepId, StepState>;

export default function Setup({
  engine,
  onBack,
  onReady,
}: {
  engine: Engine;
  onBack: () => void;
  onReady: (done: Installed) => void;
}) {
  const groups = groupsFor(engine);
  const [states, setStates] = useState(allPending);
  // The running step's latest line and bytes received, or "Already installed" once it's done.
  const [details, setDetails] = useState<Partial<Record<StepId, string>>>({});
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true; // StrictMode mounts twice in dev; the Rust side serializes the calls
    const set = (id: StepId, s: StepState) => live && setStates((prev) => ({ ...prev, [id]: s }));
    let current: StepId = "uv";
    (async () => {
      setStates(allPending());
      setDetails({});
      setError(null);
      try {
        // One channel per command: Tauri numbers each command's messages from 0 and closes the
        // channel when the command returns, so a reused channel drops the second command's steps.
        const track = () => {
          const onProgress = new Channel<Progress>();
          onProgress.onmessage = (p) => {
            current = p.step;
            set(p.step, p.done ? "done" : "running");
            if (live) setDetails((prev) => ({ ...prev, [p.step]: p.detail ?? undefined }));
          };
          return onProgress;
        };
        await invoke("install_core", { onProgress: track() });
        if (engine === "local") await invoke("install_engine", { onProgress: track() });
        current = "start";
        set("start", "running");
        const host = await invoke<HostInfo>("start_host");
        // Also proves CORS and the key work from this webview.
        const res = await fetch(`${host.url}/api/v1/models`, { headers: { "X-API-Key": host.api_key } });
        if (!res.ok) throw new Error(`The host answered ${res.status}.`);
        const { models } = (await res.json()) as { models: unknown[] };
        if (engine === "local") {
          type Health = { comfyui_connected: boolean; detail?: string };
          const health = (await (await fetch(`${host.url}/health`)).json()) as Health;
          if (!health.comfyui_connected) {
            throw new Error(`The host can't reach ComfyUI: ${health.detail ?? "no answer"}`);
          }
        }
        set("start", "done");
        if (live) onReady({ host, models: models.length });
      } catch (e) {
        set(current, "failed");
        if (live) setError(String(e));
      }
    })();
    return () => {
      live = false;
    };
  }, [attempt, engine]);

  const groupState = (rows: [StepId, string][]): StepState => {
    const all = rows.map(([id]) => states[id]);
    if (all.includes("failed")) return "failed";
    if (all.every((x) => x === "done")) return "done";
    return all.some((x) => x !== "pending") ? "running" : "pending";
  };

  const rows = groups.flatMap(([, , r]) => r);
  const finished = rows.filter(([id]) => states[id] === "done").length;
  const stopped = error?.startsWith("Stopped");
  // Stop kills the running download or install; starting the host isn't one.
  const stoppable = !error && states.start === "pending";

  return (
    <main className="page">
      <header className="hero">
        <div className="hero-title">
          <h1>Setting up</h1>
          {error ? (
            <span className="state bad">
              <Icon name="error" size={14} />
              Stopped
            </span>
          ) : (
            <span className="state now">
              <Icon name="spinner" size={14} />
              Installing
            </span>
          )}
        </div>
        <p className="lead">
          This happens once and needs no admin rights.{" "}
          {engine === "local"
            ? "It needs about 2 GB of disk on a Mac, and more with an NVIDIA or AMD GPU (their PyTorch builds are larger)."
            : "It downloads about 50 MB."}
        </p>
        <div className={error ? "progress bad" : "progress"}>
          <div className="track" role="progressbar" aria-label="Setup" aria-valuemin={0} aria-valuemax={rows.length} aria-valuenow={finished} aria-valuetext={`${finished} of ${rows.length} done`}>
            <span style={{ width: `${(finished / rows.length) * 100}%` }} />
          </div>
          <span className="cap num">
            {finished} of {rows.length} done
          </span>
        </div>
      </header>

      <ol className="steps" aria-live="polite">
        {groups.map(([title, sub, rows], n) => {
          const g = groupState(rows);
          const open = g === "running" || g === "failed";
          return (
            <li key={title} className={g === "pending" ? "step waiting" : "step"} aria-current={g === "running" ? "step" : undefined}>
              <div className="step-head">
                <span className={g === "done" ? "badge done" : g === "running" ? "badge now" : g === "failed" ? "badge bad" : "badge"}>
                  {g === "done" ? <Icon name="check" size={14} /> : g === "failed" ? <Icon name="error" size={14} /> : n + 1}
                </span>
                <span>
                  {title}
                  <small>{sub}</small>
                </span>
                <span>{{ pending: "Waiting", running: "Working…", done: "Done", failed: stopped ? "Stopped" : "Failed" }[g]}</span>
              </div>
              {open && (
                <div className="prows">
                  {rows.map(([id, label]) => (
                    <div key={id} className={states[id] === "pending" ? "prow pending" : states[id] === "failed" ? "prow failed" : "prow"}>
                      {states[id] === "done" ? (
                        <Icon name="check" size={16} />
                      ) : states[id] === "running" ? (
                        <Icon name="spinner" size={16} />
                      ) : states[id] === "failed" ? (
                        <Icon name="error" size={16} />
                      ) : (
                        <span className="dot" aria-hidden="true" />
                      )}
                      <span>
                        {label}
                        {states[id] === "running" && details[id] && <small className="live">{details[id]}</small>}
                      </span>
                      <span>{states[id] === "done" && details[id] ? details[id] : { pending: "", running: "Now", done: "Done", failed: stopped ? "Stopped" : "Failed" }[states[id]]}</span>
                    </div>
                  ))}
                </div>
              )}
            </li>
          );
        })}
      </ol>

      {error && <Alert info={stopped}>{stopped ? error : <pre className="snippet">{error}</pre>}</Alert>}

      {stoppable && (
        <footer className="page-foot">
          <span />
          <button onClick={() => invoke("stop_install")}>Stop</button>
        </footer>
      )}

      {error && (
        <footer className="page-foot">
          <button className="ghost" onClick={onBack}>
            Back
          </button>
          <button className="primary" onClick={() => setAttempt((n) => n + 1)}>
            Retry
          </button>
        </footer>
      )}
    </main>
  );
}

/** The last step: the host is up. A cloud setup adds the provider's key here first. */
export function Ready({ done, engine, onSettings }: { done: Installed; engine: Engine; onSettings: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const cloud = engine === "cloud";
  return (
    <main className="page">
      <header className="hero">
        <div className="hero-title">
          <h1>{cloud ? "Add your API key" : "All set"}</h1>
          <span className="state">
            <Icon name="check" size={14} />
            Running
          </span>
        </div>
        <p className="lead">
          {cloud
            ? "Paste the key from your provider. It's kept in your system keychain and only sent to the provider."
            : "Pick and download a model in the dashboard. Glint Studio stays in the menu bar when you close this window."}
        </p>
      </header>

      {cloud ? (
        <ProviderKeys host={done.host} />
      ) : (
        <section className="card" aria-labelledby="host-title">
          <h2 id="host-title">Host</h2>
          <dl className="facts">
            <dt>Address</dt>
            <dd>
              <code>{done.host.url}</code>
            </dd>
            <dt>Models in the catalog</dt>
            <dd className="num">{done.models}</dd>
          </dl>
        </section>
      )}

      {error && <Alert>{error}</Alert>}

      <footer className="page-foot">
        <button className="ghost" onClick={onSettings}>
          Open settings
        </button>
        <button className="primary" onClick={() => invoke("open_dashboard").catch((e) => setError(String(e)))}>
          Open the dashboard
        </button>
      </footer>
    </main>
  );
}
