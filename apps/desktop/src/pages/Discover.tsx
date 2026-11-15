import { useEffect, useState } from "react";
import { Channel, invoke } from "@tauri-apps/api/core";

// Mirror Recommendation in src-tauri/src/recommend.rs and DownloadProgress in download.rs.
type Fit = "great" | "slow" | "wont_fit";
type Recommendation = {
  id: string;
  name: string;
  license: { name: string; commercial_ok: boolean } | null;
  fit: Fit;
  reason: string;
  precision: string | null;
  bytes: number;
  installed: boolean;
};
type DownloadProgress = { done: number; total: number; verifying: boolean };

const FIT: Record<Fit, string> = { great: "Runs great", slow: "Runs slow", wont_fit: "Won't fit" };
const gb = (bytes: number) => `${(bytes / 1e9).toFixed(1)} GB`;

export default function Discover() {
  const [models, setModels] = useState<Recommendation[] | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [progress, setProgress] = useState<DownloadProgress | null>(null);
  const [error, setError] = useState<{ id: string | null; text: string } | null>(null);

  const refresh = () =>
    invoke<Recommendation[]>("recommend_models")
      .then(setModels)
      .catch((e) => setError({ id: null, text: String(e) }));
  useEffect(() => {
    refresh();
  }, []);

  const download = async (id: string) => {
    setActive(id);
    setProgress(null);
    setError(null);
    try {
      const onProgress = new Channel<DownloadProgress>();
      onProgress.onmessage = setProgress;
      await invoke("download_model", { id, onProgress });
      await refresh();
    } catch (e) {
      setError({ id, text: String(e) });
    } finally {
      setActive(null);
    }
  };

  return (
    <main className="page">
      <h1>Choose a model</h1>
      <p className="lead">Picked for this machine. Downloads resume if they stop, and each file is checked.</p>

      {error?.id === null && <pre className="error">{error.text}</pre>}
      {models === null && !error && <p className="lead">Checking what fits…</p>}
      {models?.length === 0 && <p className="lead">The catalog has no local models.</p>}

      <div className="models">
        {models?.map((m) => (
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
            {active === m.id && (
              <div role="status" aria-live="polite">
                <progress value={progress?.done ?? 0} max={progress?.total ?? m.bytes} />
                <p className="lead">
                  {progress?.verifying
                    ? "Checking the file…"
                    : `${gb(progress?.done ?? 0)} of ${gb(progress?.total ?? m.bytes)}`}
                </p>
              </div>
            )}
            {error?.id === m.id && <pre className="error">{error.text}</pre>}
            {m.installed ? (
              <p>✓ Installed</p>
            ) : (
              <button
                className="primary"
                onClick={() => download(m.id)}
                disabled={active !== null || m.fit === "wont_fit"}
              >
                {error?.id === m.id ? "Resume download" : `Download (${gb(m.bytes)})`}
              </button>
            )}
          </section>
        ))}
      </div>
    </main>
  );
}
