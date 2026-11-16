import { FormEvent, useEffect, useState } from "react";
import { api, Model, secs, Style } from "./api";

// GET /api/v1/jobs/{id}: jobs.view() plus seed and data once done.
type JobView = {
  id: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  queue_position: number;
  progress: number;
  elapsed_ms: number | null;
  error: { code: string; message: string } | null;
  seed?: number | null;
  data?: { b64_json?: string }[];
};
type Result = JobView & { prompt: string; model: string; style: string };

const finished = (r: Result) => ["done", "failed", "cancelled"].includes(r.status);

export default function Generate() {
  const [models, setModels] = useState<Model[]>([]);
  const [styles, setStyles] = useState<Style[]>([]);
  const [modelId, setModelId] = useState("");
  const [styleId, setStyleId] = useState("");
  const [shape, setShape] = useState(""); // "": the style's shape, else the model's default
  const [prompt, setPrompt] = useState("");
  const [seed, setSeed] = useState("");
  const [results, setResults] = useState<Result[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<{ models: Model[] }>("/api/v1/models").then((r) => {
      setModels(r.models);
      setModelId((id) => id || r.models[0]?.id || "");
    }, (e) => setError(e.message));
    api<{ styles: Style[] }>("/api/v1/styles").then((r) => setStyles(r.styles), (e) => setError(e.message));
  }, []);

  // shortcut: polls each unfinished job every second instead of following the events socket; fine for a
  // handful of jobs from one tab.
  const pending = results.filter((r) => !finished(r)).map((r) => r.id).join();
  useEffect(() => {
    if (!pending) return;
    const t = setInterval(() => {
      for (const id of pending.split(",")) {
        api<JobView>(`/api/v1/jobs/${id}`).then(
          (v) => setResults((all) => all.map((r) => (r.id === id ? { ...r, ...v, model: r.model } : r))),
          () => {},
        );
      }
    }, 1000);
    return () => clearInterval(t);
  }, [pending]);

  const model = models.find((m) => m.id === modelId);
  const usable = styles.filter((s) => !s.models || s.models.includes(modelId));

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!model) return;
    const size = shape && model.limits.sizes[shape];
    const body = {
      prompt,
      model: modelId,
      style: styleId || undefined,
      size: size ? `${size[0]}x${size[1]}` : undefined,
      seed: seed === "" ? undefined : Number(seed),
    };
    setError(null);
    api<JobView>("/api/v1/jobs", { method: "POST", body: JSON.stringify(body) }).then(
      (job) =>
        setResults((all) => [
          { ...job, progress: 0, elapsed_ms: null, error: null, prompt, model: model.name, style: styleId },
          ...all,
        ]),
      (e) => setError(e.message),
    );
  };

  return (
    <>
      <h1>Generate</h1>
      <form className="card form" onSubmit={submit}>
        <label>
          Prompt
          <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3} required maxLength={4000} />
        </label>
        <fieldset className="chips">
          <legend>Style</legend>
          {[{ id: "", name: "None" }, ...usable].map((s) => (
            <label key={s.id || "none"} className="chip">
              <input type="radio" name="style" checked={styleId === s.id} onChange={() => setStyleId(s.id)} />
              {s.name}
            </label>
          ))}
        </fieldset>
        <div className="split">
          <label>
            Model
            <select value={modelId} onChange={(e) => (setModelId(e.target.value), setShape(""))}>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                  {m.cloud ? " (cloud, may use credits)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            Shape
            <select value={shape} onChange={(e) => setShape(e.target.value)}>
              <option value="">Default</option>
              {model &&
                Object.entries(model.limits.sizes).map(([name, [w, h]]) => (
                  <option key={name} value={name}>
                    {name} ({w}×{h})
                  </option>
                ))}
            </select>
          </label>
          {model?.limits.supports_seed && (
            <label>
              Seed (blank for random)
              <input type="number" min={0} value={seed} onChange={(e) => setSeed(e.target.value)} />
            </label>
          )}
        </div>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="actions">
          <button className="primary" type="submit" disabled={!model || !prompt.trim()}>
            Generate
          </button>
        </div>
      </form>

      {results.length > 0 && (
        <div className="gallery">
          {results.map((r) => (
            <section key={r.id} className="card shot">
              {r.data?.map((d, i) =>
                d.b64_json ? <img key={i} src={`data:image/png;base64,${d.b64_json}`} alt={r.prompt} /> : null,
              )}
              {r.status === "running" && <progress value={r.progress} max={1} aria-label="Progress" />}
              <p className="lead">{r.prompt}</p>
              <p className="muted" role="status">
                {r.model}
                {r.style && ` · ${r.style}`}
                {r.status === "queued" && ` · waiting${r.queue_position ? ` (#${r.queue_position})` : ""}`}
                {r.status === "done" && ` · ${secs(r.elapsed_ms)}`}
                {r.seed != null && ` · seed ${r.seed}`}
                {r.status === "cancelled" && " · cancelled"}
              </p>
              {r.error && <p className="error">{r.error.message}</p>}
              {r.data?.map((d, i) =>
                d.b64_json ? (
                  <a key={i} className="save" href={`data:image/png;base64,${d.b64_json}`} download={`glint-${r.seed ?? r.id}-${i + 1}.png`}>
                    Save image{r.data!.length > 1 ? ` ${i + 1}` : ""}
                  </a>
                ) : null,
              )}
              {!finished(r) && (
                <button onClick={() => api(`/api/v1/jobs/${r.id}`, { method: "DELETE" }).catch((e) => setError(e.message))}>
                  Cancel
                </button>
              )}
            </section>
          ))}
        </div>
      )}
    </>
  );
}
