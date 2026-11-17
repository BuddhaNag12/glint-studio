import { FormEvent, useEffect, useState } from "react";
import { api, gb } from "./api";
import { Alert, PageHeader, Progress, useConfirm } from "./ui";

// Mirror host/app/bench.py: a result row, the run's view(), and GET /api/v1/bench.
type Row = {
  run: number;
  date: number;
  model: string;
  name: string;
  width: number;
  height: number;
  steps: number | null;
  runs: number;
  largest: boolean;
  s_per_image: number | null;
  s_per_step: number | null;
  peak_vram: number | null;
  error: { code: string; message: string } | null;
};
type Run = {
  id: number;
  status: "running" | "done" | "failed" | "cancelled";
  model: string;
  current: string | null;
  done: number;
  total: number;
  progress: number;
  rows: Row[];
  error: string | null;
};
type BenchModel = { id: string; name: string; steps: number | null; step_limits: [number, number] | null; sizes: [number, number][] };
type BenchData = { run: Run | null; results: Row[]; models: BenchModel[] };

const key = ([w, h]: [number, number]) => `${w}x${h}`;
const num = (v: number | null, digits: number) => (v == null ? "–" : `${v.toFixed(digits)} s`);
const ENDED = { done: "Finished.", failed: "The benchmark stopped.", cancelled: "Cancelled.", running: "" };

export default function Bench() {
  const [data, setData] = useState<BenchData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [model, setModel] = useState("");
  const [sizes, setSizes] = useState<string[]>([]);
  const [steps, setSteps] = useState("");
  const [runs, setRuns] = useState("2");
  const [fits, setFits] = useState(false);
  const confirm = useConfirm();
  const run = data?.run;
  const running = run?.status === "running";
  const picked = data?.models.find((m) => m.id === model);

  const load = () => api<BenchData>("/api/v1/bench").then(setData, (e) => setError(e.message));
  useEffect(() => {
    load();
    if (!running) return;
    const timer = setInterval(load, 1000);
    return () => clearInterval(timer);
  }, [running]);

  // A model's own sizes and steps; up to 1024² ticked to start with.
  const choose = (m: BenchModel | undefined) => {
    setModel(m?.id ?? "");
    setSizes((m?.sizes ?? []).filter(([w, h]) => w * h <= 1024 * 1024).map(key));
    setSteps("");
  };
  useEffect(() => {
    if (data && !picked) choose(data.models[0]);
  }, [data, picked]);

  const start = async (e: FormEvent) => {
    e.preventDefault();
    const ok = await confirm.ask({
      title: "Run the benchmark?",
      body: "It keeps the GPU busy for a few minutes. Images you ask for meanwhile wait their turn.",
      action: "Run",
    });
    if (!ok || !picked) return;
    setError(null);
    const body = {
      model,
      sizes: picked.sizes.filter((s) => sizes.includes(key(s))),
      steps: steps ? Number(steps) : null,
      runs: Number(runs) || 2,
      fits,
    };
    api("/api/v1/bench", { method: "POST", body: JSON.stringify(body) }).then(load, (e) => setError(e.message));
  };
  const cancel = () => api("/api/v1/bench", { method: "DELETE" }).then(load, (e) => setError(e.message));

  const rows = [...(data?.results ?? []), ...(run?.status === "running" ? run.rows : [])].reverse(); // newest first

  return (
    <>
      {confirm.dialog}
      <PageHeader title="Bench" lead="How fast each downloaded model makes images on this computer" />
      {error && <Alert>{error}</Alert>}
      <section className="card" aria-labelledby="bench-run">
        <h2 id="bench-run">Run a benchmark</h2>
        {data && data.models.length === 0 ? (
          <p className="muted">Download a model on the Models page first.</p>
        ) : (
          <form className="form" onSubmit={start}>
            <div className="two">
              <label>
                Model
                <select value={model} disabled={running} onChange={(e) => choose(data?.models.find((m) => m.id === e.target.value))}>
                  {data?.models.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Steps
                <input
                  type="number"
                  value={steps}
                  disabled={running || !picked?.step_limits}
                  min={picked?.step_limits?.[0]}
                  max={picked?.step_limits?.[1]}
                  placeholder={picked?.steps != null ? `${picked.steps} (default)` : "Default"}
                  onChange={(e) => setSteps(e.target.value)}
                />
              </label>
              <label>
                Runs per size
                <input type="number" min={1} max={10} value={runs} disabled={running} onChange={(e) => setRuns(e.target.value)} aria-describedby="bench-runs-help" />
                <span id="bench-runs-help" className="help">
                  With more than one, the first warms up the model and isn't counted.
                </span>
              </label>
            </div>
            <fieldset className="chips" disabled={running}>
              <legend>Sizes</legend>
              <div className="chip-row">
                {picked?.sizes.map((s) => (
                  <label key={key(s)} className="chip">
                    <input
                      type="checkbox"
                      checked={sizes.includes(key(s))}
                      onChange={(e) => setSizes(e.target.checked ? [...sizes, key(s)] : sizes.filter((x) => x !== key(s)))}
                    />
                    {s[0]}×{s[1]}
                  </label>
                ))}
              </div>
            </fieldset>
            <label className="toggle">
              <input type="checkbox" role="switch" className="switch" checked={fits} disabled={running} onChange={(e) => setFits(e.target.checked)} />
              Find the largest size that fits
            </label>
            <div className="actions">
              {running ? (
                <button type="button" onClick={cancel}>
                  Cancel
                </button>
              ) : (
                <button type="submit" className="primary" disabled={!picked || (!sizes.length && !fits)}>
                  Run
                </button>
              )}
            </div>
          </form>
        )}
        {run && (
          <div role="status">
            {running ? (
              <>
                <Progress value={run.progress} label="Benchmark progress" />
                <p className="help">
                  {run.current ?? "Starting"} · image {Math.min(run.done + 1, run.total)} of {run.total}
                </p>
              </>
            ) : (
              <p className="help">{ENDED[run.status]}</p>
            )}
            {run.error && <Alert>{run.error}</Alert>}
          </div>
        )}
      </section>
      <section className="card" aria-labelledby="bench-results">
        <div className="card-head">
          <div>
            <h2 id="bench-results">Results</h2>
            <p className="muted small">Results stay on this computer and tune the speed labels on Models.</p>
          </div>
        </div>
        {rows.length === 0 ? (
          <p className="muted">No results yet.</p>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col">Size</th>
                <th scope="col" className="n">Steps</th>
                <th scope="col" className="n">s/image</th>
                <th scope="col" className="n">s/step</th>
                <th scope="col" className="n">Peak VRAM</th>
                <th scope="col">Date</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={`${r.run}-${i}`}>
                  <td>{r.name}</td>
                  <td>
                    {r.width}×{r.height}
                    {r.largest && <span className="muted"> (largest that fits)</span>}
                  </td>
                  <td className="n">{r.steps ?? "–"}</td>
                  {r.error && r.s_per_image == null ? (
                    <td colSpan={3} className="muted">
                      Failed: {r.error.message}
                    </td>
                  ) : (
                    <>
                      <td className="n">{num(r.s_per_image, 1)}</td>
                      <td className="n">{num(r.s_per_step, 2)}</td>
                      <td className="n">{r.peak_vram == null ? "–" : gb(r.peak_vram)}</td>
                    </>
                  )}
                  <td>{new Date(r.date * 1000).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
