import { FormEvent, useEffect, useState } from "react";
import { api, gb, Hardware, System } from "./api";
import { Alert, Fit, FitGauge, FitLabel, Icon, PageHeader, Progress, Tabs } from "./ui";

// Mirror recommend_one() and Downloads.view() in host/app/local_models.py.
type Download = { status: "downloading" | "done" | "failed" | "cancelled"; done: number; total: number; verifying: boolean; error: string | null };
type LocalModel = {
  id: string;
  name: string;
  license: { name: string; commercial_ok: boolean } | null;
  fit: Fit;
  reason: string;
  precision: string;
  min_vram_gb: number;
  bytes: number;
  installed: boolean;
  download: Download | null;
};

/** `tab` comes from the address (#models/installed), so the header menu can open either view. */
export default function Models({ tab, system }: { tab: "discover" | "installed"; system: System | null }) {
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

  const active = models?.find((m) => m.download?.status === "downloading");
  const busy = !!active;
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

  const installed = models?.filter((m) => m.installed) ?? [];
  const shown = tab === "installed" ? installed : models ?? [];
  // The gauge needs a GPU budget to measure against; on CPU it would only say "everything is slow".
  const gauge = hw && hw.backend !== "cpu" && hw.vram_budget_gb > 0;
  const d = active?.download;

  return (
    <>
      <PageHeader
        title="Models"
        lead="Image models picked to fit this computer"
      >
        <Tabs
          label="Show"
          value={tab}
          onChange={(t) => (location.hash = `models/${t}`)}
          options={[
            ["discover", "Discover"],
            ["installed", <span key="i" className="num">Installed ({installed.length})</span>],
          ]}
        />
      </PageHeader>

      {error && <Alert>{error}</Alert>}
      {hw && <ThisComputer hw={hw} system={system} />}
      <EngineCard system={system} models={installed} onFolders={refresh} />

      {active && d && (
        <div className="card download-bar" role="status" aria-live="polite">
          <span>
            <strong>{active.name}</strong>
            <span className="muted num">
              {" · "}
              {d.verifying ? "Checking the files…" : `${gb(d.done)} of ${gb(d.total)}`}
            </span>
          </span>
          <Progress value={d.total ? d.done / d.total : 0} label={`Downloading ${active.name}`} />
          <button className="ghost small" onClick={() => act(active.id, "DELETE")}>
            Cancel
          </button>
        </div>
      )}

      <section className="card list-card" aria-labelledby="list-title">
        <div className="card-head">
          <h2 id="list-title">{tab === "installed" ? "On this computer" : "Recommended for you"}</h2>
          <span className="cap">Downloads resume if they stop, and every file is checked</span>
        </div>
        {models === null && !error && <p className="model-row muted">Checking what fits…</p>}
        {models && shown.length === 0 && (
          <p className="model-row muted">{tab === "installed" ? "Nothing installed yet. Pick one from Discover." : "The catalog has no local models."}</p>
        )}
        {shown.map((m) => {
          const dl = m.download;
          return (
            <div key={m.id} className={m.fit === "wont_fit" && !m.installed ? "model-row off" : "model-row"}>
              <div>
                <div className="model-name">
                  <strong>{m.name}</strong>
                  <span className="tag">{m.precision}</span>
                  {m.license && (
                    <span className="tag" title={m.license.name}>
                      {m.license.commercial_ok ? "Commercial use ok" : "Non-commercial"}
                    </span>
                  )}
                </div>
                <span className="muted small num">
                  {m.reason} · {gb(m.bytes)}
                </span>
                {dl?.status === "failed" && dl.error && <span className="error small">{dl.error}</span>}
              </div>
              <div className="fit">
                {gauge && <FitGauge fit={m.fit} need={m.min_vram_gb} budget={hw.vram_budget_gb} />}
                <FitLabel fit={m.fit}>{gauge && ` · needs ${m.min_vram_gb.toFixed(1)} of ${hw.vram_budget_gb.toFixed(1)} GB`}</FitLabel>
              </div>
              <div>
                {m.installed ? (
                  <span className="status">
                    <Icon name="check" size={14} />
                    Installed
                  </span>
                ) : dl?.status === "downloading" ? (
                  <span className="status quiet num">
                    {dl.total ? Math.round((dl.done / dl.total) * 100) : 0}%
                    <button className="icon" aria-label={`Cancel ${m.name} download`} onClick={() => act(m.id, "DELETE")}>
                      <Icon name="x" />
                    </button>
                  </span>
                ) : (
                  <button className="small" onClick={() => act(m.id, "POST")} disabled={busy || m.fit === "wont_fit"}>
                    <Icon name="download" size={16} />
                    {dl?.status === "failed" || dl?.status === "cancelled" ? "Resume" : "Download"}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </section>
    </>
  );
}

/** What this computer can give models: the card at the top of Models. */
function ThisComputer({ hw, system }: { hw: Hardware; system: System | null }) {
  const gpu = hw.gpus?.[0];
  const unified = hw.backend === "mps";
  // Apple Silicon shares one pool with macOS; elsewhere the budget comes out of the GPU's own memory.
  const total = unified ? hw.ram_gb : (gpu?.vram_gb ?? 0);
  const budget = Math.min(hw.vram_budget_gb, total);
  const bar = hw.backend !== "cpu" && budget > 0;
  const rest = unified ? "macOS and other apps" : "Kept free";
  const sub =
    hw.backend === "cpu"
      ? `${hw.ram_gb.toFixed(0)} GB memory · no supported GPU, so images run on the processor`
      : unified
        ? `${hw.ram_gb.toFixed(0)} GB unified memory · runs on Metal (MPS)`
        : `${gpu?.vram_gb ? `${gpu.vram_gb.toFixed(0)} GB graphics memory · ` : ""}${hw.ram_gb.toFixed(0)} GB system memory`;
  const devices = system?.engine.up ? system.engine.devices : [];

  return (
    <section className="card hw" aria-labelledby="hw-title">
      <div className="hw-top">
        <div className="hw-id">
          <span className="hw-icon" aria-hidden="true">
            <Icon name="cpu" size={24} />
          </span>
          <div>
            <span className="hw-label">This computer</span>
            <h2 id="hw-title">{gpu?.name ?? hw.cpu ?? "Unknown processor"}</h2>
            <span className="muted small num">{sub}</span>
          </div>
        </div>
        <div className="hw-budget">
          <span className="hw-label">Memory for models</span>
          <span className="figure num">
            {hw.vram_budget_gb.toFixed(1)}
            <small> GB</small>
          </span>
        </div>
      </div>

      {bar && (
        <div>
          <div className="stackbar" role="img" aria-label={`${budget.toFixed(1)} of ${total.toFixed(1)} GB for models, ${(total - budget).toFixed(1)} GB ${rest.toLowerCase()}`}>
            <span className="seg on" style={{ flexGrow: budget }} />
            {total > budget && <span className="seg" style={{ flexGrow: total - budget }} />}
          </div>
          <ul className="legend num">
            <li>
              <span className="key on" aria-hidden="true" />
              For models <strong>{budget.toFixed(1)} GB</strong>
            </li>
            {total > budget && (
              <li>
                <span className="key" aria-hidden="true" />
                {rest} <strong>{(total - budget).toFixed(1)} GB</strong>
              </li>
            )}
          </ul>
        </div>
      )}

      {devices.length > 0 && (
        <dl className="facts">
          {devices.map((d) => (
            <GpuMemory key={d.name} name={d.name} total={d.vram_total} free={d.vram_free} />
          ))}
        </dl>
      )}

      <div className="hw-foot">
        <span className="tag">Runs on {hw.backend.toUpperCase()}</span>
        {system && <span className="tag num">Models folder {gb(system.models_bytes)}</span>}
        <span className="tag num">Free disk {hw.disk_free_gb.toFixed(0)} GB</span>
        <span className="cap">Estimates. Real speed depends on image size and steps.</span>
      </div>
      {hw.warnings?.map((w) => (
        <p key={w} className="cap">
          {w}
        </p>
      ))}
    </section>
  );
}

/** Live memory use the engine reports for one device. */
function GpuMemory({ name, total, free }: { name: string; total: number; free: number }) {
  return (
    <>
      <dt>{name.replace(/^cuda:\d+\s*/, "").split(" : ")[0] || name} in use now</dt>
      <dd>
        <div className="meter">
          <span className="num">
            {gb(total - free)} of {gb(total)}
          </span>
          <Progress value={(total - free) / total} label="GPU memory in use" />
        </div>
      </dd>
    </>
  );
}

// Mirrors engine_settings() in host/app/dashboard.py.
type Folder = { path: string; models: number; exists: boolean };
type EngineSettings = { preload: boolean; folders: Folder[]; found: Folder[] };

/** The model kept in memory, loading one at start, and model folders from a ComfyUI already on this computer. */
function EngineCard({ system, models, onFolders }: { system: System | null; models: LocalModel[]; onFolders: () => void }) {
  const [settings, setSettings] = useState<EngineSettings | null>(null);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<EngineSettings>("/api/v1/engine/settings").then(setSettings, (e) => setError(e.message));
  }, []);

  const save = (body: { preload?: boolean; folders?: string[] }) => {
    setBusy(true);
    return api<EngineSettings>("/api/v1/engine/settings", { method: "PUT", body: JSON.stringify(body) })
      .then(
        (s) => {
          setSettings(s);
          setError(null);
          if (body.folders) onFolders(); // linked models count as installed
          return true;
        },
        (e) => (setError(e.message), false),
      )
      .finally(() => setBusy(false));
  };
  const folders = settings?.folders.map((f) => f.path) ?? [];
  const link = (p: string) => save({ folders: [...folders, p] });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (path.trim()) link(path).then((ok) => ok && setPath(""));
  };
  const engine = (action: "load" | "unload") =>
    api(`/api/v1/engine/${action}`, { method: "POST" }).then(() => setError(null), (e) => setError(e.message));

  const state = system?.engine.model;
  const name = models.find((m) => m.id === state?.model)?.name ?? state?.model;
  const up = !!system?.engine.up;

  return (
    <section className="card" aria-labelledby="engine-title">
      <div className="card-head">
        <h2 id="engine-title">Model in memory</h2>
        <span className="cap">The first image after loading skips the wait</span>
      </div>
      <div className="memory-row">
        <div role="status">
          {!up ? (
            <span className="muted">The engine isn't running.</span>
          ) : state?.status === "loading" ? (
            <span className="status quiet">
              <Icon name="spinner" size={14} />
              Loading {name}… this takes a minute or two
            </span>
          ) : state?.status === "loaded" ? (
            <span className="status">
              <Icon name="check" size={14} />
              {name} is loaded and ready
            </span>
          ) : (
            <span className="muted">{state?.status === "failed" ? `Couldn't load ${name}: ${state.error}` : "No model loaded. The next image loads one first."}</span>
          )}
        </div>
        {up &&
          (state?.status === "loaded" || state?.status === "loading" ? (
            <button className="small" onClick={() => engine("unload")}>
              Unload
            </button>
          ) : (
            <button className="small" onClick={() => engine("load")} disabled={models.length === 0}>
              Load now
            </button>
          ))}
      </div>
      {settings && (
        <div className="share-row">
          <label className="toggle">
            <input type="checkbox" className="switch" role="switch" checked={settings.preload} disabled={busy} onChange={(e) => save({ preload: e.target.checked })} />
            Load a model when Glint Studio starts
          </label>
          <p className="help">Keeps it in memory until you unload it. Uses the default model, or the first one installed.</p>
        </div>
      )}

      <h3 className="sub-title">
        <Icon name="folder" size={16} />
        Models from your ComfyUI
      </h3>
      <p className="muted small">Already have ComfyUI? Link its models folder and Glint Studio uses those files instead of downloading them again. Linking restarts the engine.</p>
      {error && <Alert>{error}</Alert>}
      {settings && (
        <ul className="folders">
          {settings.folders.map((f) => (
            <li key={f.path}>
              <code>{f.path}</code>
              <span className="muted small num">{f.exists ? `${f.models} model files` : "Not found"}</span>
              <button className="ghost small" disabled={busy} onClick={() => save({ folders: folders.filter((p) => p !== f.path) })}>
                Remove
              </button>
            </li>
          ))}
          {settings.found.map((f) => (
            <li key={f.path} className="found">
              <code>{f.path}</code>
              <span className="muted small num">Found · {f.models} model files</span>
              <button className="small" disabled={busy} onClick={() => link(f.path)}>
                Link
              </button>
            </li>
          ))}
        </ul>
      )}
      <form className="link-folder" onSubmit={submit}>
        <label className="sr-only" htmlFor="folder-path">
          ComfyUI folder
        </label>
        <input id="folder-path" value={path} onChange={(e) => setPath(e.target.value)} placeholder={"Paste a folder, e.g. C:\\ComfyUI_windows_portable\\ComfyUI"} />
        <button type="submit" className="small" disabled={busy || !path.trim()}>
          Link folder
        </button>
      </form>
    </section>
  );
}
