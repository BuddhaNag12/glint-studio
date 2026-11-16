import { useEffect, useState } from "react";
import { api, gb, Hardware } from "./api";

// Mirror recommend_one() and Downloads.view() in host/app/local_models.py.
type Fit = "great" | "slow" | "wont_fit";
type Download = { status: "downloading" | "done" | "failed" | "cancelled"; done: number; total: number; verifying: boolean; error: string | null };
type LocalModel = {
  id: string;
  name: string;
  license: { name: string; commercial_ok: boolean } | null;
  fit: Fit;
  reason: string;
  precision: string;
  bytes: number;
  installed: boolean;
  download: Download | null;
};

const FIT: Record<Fit, string> = { great: "Runs great", slow: "Runs slow", wont_fit: "Won't fit" };

export default function Models() {
  const [models, setModels] = useState<LocalModel[] | null>(null);
  const [hw, setHw] = useState<Hardware | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = () =>
    api<{ hardware: Hardware; models: LocalModel[] }>("/api/v1/local-models").then(
      (r) => {
        setModels(r.models);
        setHw(r.hardware);
        setError(null);
      },
      (e) => setError(e.message),
    );

  const busy = models?.some((m) => m.download?.status === "downloading") ?? false;
  useEffect(() => {
    refresh();
  }, []);
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(refresh, 1000);
    return () => clearInterval(t);
  }, [busy]);

  const act = (id: string, method: "POST" | "DELETE") =>
    api(`/api/v1/local-models/${id}/download`, { method }).then(refresh, (e) => setError(e.message));

  return (
    <>
      <h1>Models</h1>
      <p className="lead">
        Picked for this machine
        {hw && ` (${hw.backend.toUpperCase()}, ${hw.vram_budget_gb.toFixed(0)} GB for models, ${hw.disk_free_gb.toFixed(0)} GB free disk)`}.
        Downloads resume if they stop, and each file is checked.
      </p>
      {error && <p className="error">{error}</p>}
      {models === null && !error && <p className="lead">Checking what fits…</p>}
      {models?.length === 0 && <p className="lead">The catalog has no local models.</p>}

      <div className="models">
        {models?.map((m) => {
          const d = m.download;
          return (
            <section key={m.id} className="card model">
              <div className="model-head">
                <h2>{m.name}</h2>
                <span className={`fit ${m.fit}`}>{FIT[m.fit]}</span>
              </div>
              <p className="lead">
                {m.reason}
                <br />
                {m.precision} · {gb(m.bytes)}
                {m.license && ` · ${m.license.name}${m.license.commercial_ok ? "" : " (non-commercial)"}`}
              </p>
              {d?.status === "downloading" && (
                <div role="status" aria-live="polite">
                  <progress value={d.done} max={d.total} />
                  <p className="lead">{d.verifying ? "Checking the file…" : `${gb(d.done)} of ${gb(d.total)}`}</p>
                </div>
              )}
              {d?.status === "failed" && <pre className="error">{d.error}</pre>}
              {m.installed ? (
                <p>✓ Installed</p>
              ) : d?.status === "downloading" ? (
                <button onClick={() => act(m.id, "DELETE")}>Cancel</button>
              ) : (
                <button className="primary" onClick={() => act(m.id, "POST")} disabled={busy || m.fit === "wont_fit"}>
                  {d?.status === "failed" || d?.status === "cancelled" ? "Resume download" : `Download (${gb(m.bytes)})`}
                </button>
              )}
            </section>
          );
        })}
      </div>
    </>
  );
}
