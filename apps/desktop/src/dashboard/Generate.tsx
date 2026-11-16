import { FormEvent, useEffect, useRef, useState } from "react";
import { api, JobEvent, Model, onJobEvents, Quality, secs, Style } from "./api";

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

const finished = (r: { status: string }) => ["done", "failed", "cancelled"].includes(r.status);
const QUALITY: Record<Quality, string> = { fast: "Fast", standard: "Standard", best: "Best" };

export default function Generate() {
  const [models, setModels] = useState<Model[]>([]);
  const [styles, setStyles] = useState<Style[]>([]);
  const [modelId, setModelId] = useState("");
  const [styleId, setStyleId] = useState("");
  const [shape, setShape] = useState(""); // "": the style's shape, else the model's default
  const [quality, setQuality] = useState<Quality | "">("");
  const [prompt, setPrompt] = useState("");
  const [seed, setSeed] = useState("");
  const [steps, setSteps] = useState("");
  const [cfg, setCfg] = useState("");
  const [negative, setNegative] = useState("");
  const [results, setResults] = useState<Result[]>([]);
  const [error, setError] = useState<string | null>(null);
  const live = useRef(results);
  live.current = results;

  useEffect(() => {
    api<{ models: Model[] }>("/api/v1/models").then((r) => {
      setModels(r.models);
      setModelId((id) => id || r.models[0]?.id || "");
    }, (e) => setError(e.message));
    api<{ styles: Style[] }>("/api/v1/styles").then((r) => setStyles(r.styles), (e) => setError(e.message));
  }, []);

  // A finished job never goes back to unfinished, whichever of the event and the fetch lands last.
  const merge = (id: string, v: Partial<JobView>) =>
    setResults((all) =>
      all.map((r) => (r.id !== id || (finished(r) && !finished({ status: v.status ?? r.status })) ? r : { ...r, ...v, model: r.model })),
    );
  const refresh = (id: string) => api<JobView>(`/api/v1/jobs/${id}`).then((v) => merge(id, v), () => {});

  // Events carry progress; the image and seed come from the job itself once it's done. After a reconnect,
  // catch up on every unfinished job, since its events may have been missed.
  useEffect(
    () =>
      onJobEvents(
        (e: JobEvent) => {
          merge(e.id, e);
          if (finished(e)) refresh(e.id);
        },
        () => live.current.filter((r) => !finished(r)).forEach((r) => refresh(r.id)),
      ),
    [],
  );

  const model = models.find((m) => m.id === modelId);
  const usable = styles.filter((s) => !s.models || s.models.includes(modelId));
  const qualities = (Object.keys(QUALITY) as Quality[]).filter((q) => model?.quality[q]);
  const preset = quality ? model?.quality[quality] : undefined;
  const pickModel = (id: string) => {
    setModelId(id);
    setShape("");
    setQuality("");
    setSteps("");
    setCfg("");
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!model) return;
    const size = shape && model.limits.sizes[shape];
    const num = (v: string) => (v === "" ? undefined : Number(v));
    const body = {
      prompt,
      model: modelId,
      style: styleId || undefined,
      size: size ? `${size[0]}x${size[1]}` : undefined,
      quality: quality || undefined,
      seed: num(seed),
      steps: num(steps),
      cfg: num(cfg),
      negative_prompt: negative.trim() || undefined,
    };
    setError(null);
    api<JobView>("/api/v1/jobs", { method: "POST", body: JSON.stringify(body) }).then(
      (job) => {
        setResults((all) => [
          { ...job, progress: 0, elapsed_ms: null, error: null, prompt, model: model.name, style: styleId },
          ...all,
        ]);
        refresh(job.id); // in case it finished before it was in the list
      },
      (e) => setError(e.message),
    );
  };

  const [minSteps, maxSteps] = model?.limits.steps ?? [];
  const [minCfg, maxCfg] = model?.limits.cfg ?? [];

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
            <select value={modelId} onChange={(e) => pickModel(e.target.value)}>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                  {m.cloud ? " (cloud, may use credits)" : ""}
                </option>
              ))}
            </select>
          </label>
          {qualities.length > 0 && (
            <label>
              Quality
              <select value={quality} onChange={(e) => setQuality(e.target.value as Quality | "")}>
                <option value="">Default</option>
                {qualities.map((q) => (
                  <option key={q} value={q}>
                    {QUALITY[q]}
                  </option>
                ))}
              </select>
            </label>
          )}
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
              Variation number (blank for random)
              <input type="number" min={0} value={seed} onChange={(e) => setSeed(e.target.value)} />
            </label>
          )}
        </div>
        {model && (model.limits.steps || model.limits.cfg || model.limits.supports_negative) && (
          <details className="advanced">
            <summary>Advanced</summary>
            <div className="split">
              {model.limits.steps && (
                <label>
                  Steps ({minSteps}–{maxSteps})
                  <input
                    type="number"
                    min={minSteps}
                    max={maxSteps}
                    value={steps}
                    placeholder={`Auto (${preset?.steps ?? model.defaults.steps ?? "model's"})`}
                    onChange={(e) => setSteps(e.target.value)}
                  />
                </label>
              )}
              {model.limits.cfg && (
                <label>
                  CFG ({minCfg}–{maxCfg})
                  <input
                    type="number"
                    min={minCfg}
                    max={maxCfg}
                    step={0.1}
                    value={cfg}
                    placeholder={`Auto (${preset?.cfg ?? model.defaults.cfg ?? "model's"})`}
                    onChange={(e) => setCfg(e.target.value)}
                  />
                </label>
              )}
            </div>
            {model.limits.supports_negative && (
              <label>
                Negative prompt (added to the model's and the style's)
                <textarea value={negative} onChange={(e) => setNegative(e.target.value)} rows={2} maxLength={4000} />
              </label>
            )}
          </details>
        )}
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
                {r.status === "running" && " · running"}
                {r.status === "done" && ` · ${secs(r.elapsed_ms)}`}
                {r.seed != null && ` · variation ${r.seed}`}
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
