import { useEffect, useState } from "react";
import { Channel, invoke } from "@tauri-apps/api/core";
import type { Engine } from "./Onboarding";
import { Alert, Icon } from "../dashboard/ui";

// Mirrors Progress and HostInfo in src-tauri/src/runtime.rs; "start" is the start_host call.
type StepId = "uv" | "python" | "venv" | "host" | "torch" | "comfyui" | "start";
type Progress = { step: Exclude<StepId, "start">; done: boolean };
export type HostInfo = { url: string; api_key: string };
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
  onReady: (host: HostInfo) => void;
}) {
  const groups = groupsFor(engine);
  const [states, setStates] = useState(allPending);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState<{ host: HostInfo; models: number } | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true; // StrictMode mounts twice in dev; the Rust side serializes the calls
    const set = (id: StepId, s: StepState) => live && setStates((prev) => ({ ...prev, [id]: s }));
    let current: StepId = "uv";
    (async () => {
      setStates(allPending());
      setError(null);
      try {
        // One channel per command: Tauri numbers each command's messages from 0 and closes the
        // channel when the command returns, so a reused channel drops the second command's steps.
        const track = () => {
          const onProgress = new Channel<Progress>();
          onProgress.onmessage = (p) => {
            current = p.step;
            set(p.step, p.done ? "done" : "running");
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
        if (live) setReady({ host, models: models.length });
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

  return (
    <main className="page">
      <div className="page-intro">
        <span className="mark large" aria-hidden="true" />
        <h1>Setting up</h1>
        <p className="lead">
          This happens once and needs no admin rights.{" "}
          {engine === "local"
            ? "It needs about 2 GB of disk on a Mac, and more with an NVIDIA or AMD GPU (their PyTorch builds are larger)."
            : "It downloads about 50 MB."}
        </p>
      </div>

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
                <span>{{ pending: "Waiting", running: "Working…", done: "Done", failed: "Failed" }[g]}</span>
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
                      <span>{label}</span>
                      <span>{{ pending: "", running: "Now", done: "Done", failed: "Failed" }[states[id]]}</span>
                    </div>
                  ))}
                </div>
              )}
            </li>
          );
        })}
      </ol>

      {error && (
        <Alert>
          <pre className="snippet">{error}</pre>
        </Alert>
      )}

      {ready && (
        <section className="card" aria-labelledby="ready-title">
          <h2 id="ready-title" className="status">
            <Icon name="check" />
            Ready
          </h2>
          <p className="num">
            The host is running at <code>{ready.host.url}</code> with {ready.models} model{ready.models === 1 ? "" : "s"} in the
            catalog.
          </p>
          {engine === "local" && <p className="muted">Pick and download a model in the dashboard. Glint Studio stays in the menu bar.</p>}
        </section>
      )}

      <div className="page-foot">
        <button className="ghost" onClick={onBack} disabled={!ready && !error}>
          Back
        </button>
        <div className="actions">
          {error && <button onClick={() => setAttempt((n) => n + 1)}>Retry</button>}
          {ready &&
            (engine === "local" ? (
              <button className="primary" onClick={() => invoke("open_dashboard").catch((e) => setError(String(e)))}>
                Open the dashboard
              </button>
            ) : (
              <button className="primary" onClick={() => onReady(ready.host)}>
                Add your API key
              </button>
            ))}
        </div>
      </div>
    </main>
  );
}
