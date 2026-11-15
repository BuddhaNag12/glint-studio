import { useEffect, useState } from "react";
import { Channel, invoke } from "@tauri-apps/api/core";
import type { Engine } from "./Onboarding";

// Mirrors Progress and HostInfo in src-tauri/src/runtime.rs; "start" is the start_host call.
type StepId = "uv" | "python" | "venv" | "host" | "start";
type Progress = { step: Exclude<StepId, "start">; done: boolean };
export type HostInfo = { url: string; api_key: string };
type StepState = "pending" | "running" | "done" | "failed";

const STEPS: [StepId, string][] = [
  ["uv", "Get the package manager (uv)"],
  ["python", "Install Python 3.12"],
  ["venv", "Create Glint Studio's own environment"],
  ["host", "Install the Glint Studio host"],
  ["start", "Start the host"],
];
const MARK: Record<StepState, string> = { pending: "○", running: "◐", done: "✓", failed: "✕" };
const allPending = () => Object.fromEntries(STEPS.map(([id]) => [id, "pending"])) as Record<StepId, StepState>;

export default function Setup({ engine, onBack, onReady }: { engine: Engine; onBack: () => void; onReady: (host: HostInfo) => void }) {
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
        const onProgress = new Channel<Progress>();
        onProgress.onmessage = (p) => {
          current = p.step;
          set(p.step, p.done ? "done" : "running");
        };
        await invoke("install_core", { onProgress });
        current = "start";
        set("start", "running");
        const host = await invoke<HostInfo>("start_host");
        // Also proves CORS and the key work from this webview.
        const res = await fetch(`${host.url}/api/v1/models`, { headers: { "X-API-Key": host.api_key } });
        if (!res.ok) throw new Error(`The host answered ${res.status}.`);
        const { models } = (await res.json()) as { models: unknown[] };
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
  }, [attempt]);

  return (
    <main className="page">
      <h1>Setting up</h1>
      <p className="lead">This happens once. It downloads about 50 MB and needs no admin rights.</p>

      <section className="card" aria-live="polite">
        <ol className="steps">
          {STEPS.map(([id, label]) => (
            <li key={id} className={states[id]}>
              <span className="mark" aria-hidden="true">{MARK[states[id]]}</span>
              {label}
              <span className="visually-hidden">: {states[id]}</span>
            </li>
          ))}
        </ol>
        {error && (
          <>
            <pre className="error">{error}</pre>
            <button onClick={() => setAttempt((n) => n + 1)}>Retry</button>
          </>
        )}
      </section>

      {ready && (
        <section className="card ready">
          <h2>Ready</h2>
          <p>
            The host is running at {ready.host.url} with {ready.models} model{ready.models === 1 ? "" : "s"} in
            the catalog.
          </p>
          {/* shortcut: the next build adds the engine install (local) here. */}
          {engine === "local" ? (
            <p className="lead">Next: installing the image engine (PyTorch and ComfyUI) comes in the next build.</p>
          ) : (
            <button className="primary" onClick={() => onReady(ready.host)}>
              Add your API key
            </button>
          )}
        </section>
      )}

      <button className="back" onClick={onBack} disabled={!ready && !error}>
        Back
      </button>
    </main>
  );
}
