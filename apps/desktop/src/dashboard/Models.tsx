import { FormEvent, KeyboardEvent, useEffect, useState } from "react";
import { api, gb, Hardware, System } from "./api";
import { Alert, Fit, FitGauge, FitLabel, Icon, Modal, PageHeader, Progress, Tabs, useConfirm } from "./ui";

// Mirror recommend_one(), add_tiers(), memory_split() and Downloads.view() in host/app/local_models.py.
type Download = { status: "downloading" | "done" | "failed" | "cancelled"; done: number; total: number; verifying: boolean; error: string | null };
type Tier = "best" | "better" | "faster" | "other";
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
  tier: Tier;
  speed_note: string | null;
  s_per_image: number | null; // measured by a benchmark run
  download: Download | null;
};
type Memory = { unified: boolean; total_gb: number; models_gb: number; headroom_gb: number; system_gb: number };

const TIERS: [Tier, string, string][] = [
  ["best", "Best for this computer", "Runs well here, best quality first"],
  ["better", "Better quality, slower", "Too big to run fully on the GPU, so each image takes longer"],
  ["faster", "Faster, lighter", "Quicker images, a step down in quality"],
  ["other", "Other models", "Too slow or too big for this computer"],
];

/** `tab` comes from the address (#models/installed), so the header menu can open either view. */
export default function Models({ tab, system }: { tab: "discover" | "installed"; system: System | null }) {
  const [models, setModels] = useState<LocalModel[] | null>(null);
  const [hw, setHw] = useState<Hardware | null>(null);
  const [memory, setMemory] = useState<Memory | null>(null);
  const [unlock, setUnlock] = useState<string[]>([]);
  const [editing, setEditing] = useState<LocalModel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const confirm = useConfirm();

  const refresh = () =>
    api<{ hardware: Hardware; models: LocalModel[]; memory: Memory; unlock: string[] }>("/api/v1/local-models").then(
      (r) => {
        setModels(r.models);
        setHw(r.hardware);
        setMemory(r.memory);
        setUnlock(r.unlock);
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

  const remove = async (m: LocalModel) => {
    const ok = await confirm.ask({
      title: `Delete ${m.name}?`,
      body: `Frees up to ${gb(m.bytes)}. Files another installed model uses stay. You can download it again later.`,
      action: "Delete",
    });
    if (ok) api(`/api/v1/local-models/${m.id}`, { method: "DELETE" }).then(refresh, (e) => setError(e.message));
  };

  const installed = models?.filter((m) => m.installed) ?? [];
  // The gauge needs a GPU budget to measure against; on CPU it would only say "everything is slow".
  const budget = hw && hw.backend !== "cpu" && hw.vram_budget_gb > 0 ? hw.vram_budget_gb : 0;
  const d = active?.download;
  const row = (m: LocalModel) => <ModelRow key={m.id} m={m} budget={budget} busy={busy} act={act} onSettings={() => setEditing(m)} onDelete={() => remove(m)} />;
  const unlockNames = models?.filter((m) => unlock.includes(m.id)).map((m) => m.name) ?? [];

  return (
    <>
      <PageHeader title="Models" lead="Image models picked to fit this computer">
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
      {hw && memory && <ThisComputer hw={hw} memory={memory} system={system} />}
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

      {tab === "installed" || !models?.length ? (
        <section className="card list-card" aria-labelledby="list-title">
          <div className="card-head">
            <h2 id="list-title">{tab === "installed" ? "On this computer" : "Recommended for you"}</h2>
            <span className="cap">Downloads resume if they stop, and every file is checked</span>
          </div>
          {models === null && !error && <p className="model-row muted">Checking what fits…</p>}
          {models && (tab === "installed" ? installed : models).length === 0 && (
            <p className="model-row muted">{tab === "installed" ? "Nothing installed yet. Pick one from Discover." : "The catalog has no local models."}</p>
          )}
          {tab === "installed" && installed.map(row)}
        </section>
      ) : (
        <>
          {TIERS.map(([tier, title, cap]) => {
            const rows = models.filter((m) => m.tier === tier);
            return (
              rows.length > 0 && (
                <section key={tier} className="card list-card" aria-labelledby={`tier-${tier}`}>
                  <div className="card-head">
                    <h2 id={`tier-${tier}`}>{title}</h2>
                    <span className="cap">{cap}</span>
                  </div>
                  {rows.map(row)}
                </section>
              )
            );
          })}
          {unlockNames.length > 0 && (
            <section className="card">
              <details className="more">
                <summary>
                  Unlock bigger models
                  <Icon name="chevron" size={16} />
                </summary>
                <p className="muted small">
                  {unlockNames.join(", ")} would run fully on the GPU if models could use more memory. Raise the memory guard in{" "}
                  <a href="#settings/engine">Settings → Engine</a>. Less memory is then left for other apps.
                </p>
              </details>
            </section>
          )}
        </>
      )}
      {editing && <ModelSettings model={editing} onClose={() => setEditing(null)} />}
      {confirm.dialog}
    </>
  );
}

function ModelRow({
  m,
  budget,
  busy,
  act,
  onSettings,
  onDelete,
}: {
  m: LocalModel;
  budget: number;
  busy: boolean;
  act: (id: string, method: "POST" | "DELETE") => void;
  onSettings: () => void;
  onDelete: () => void;
}) {
  const dl = m.download;
  return (
    <div className={m.fit === "wont_fit" && !m.installed ? "model-row off" : "model-row"}>
      <div className="model-head">
        <div className="model-name">
          <strong>{m.name}</strong>
          <span className="tag">{m.precision}</span>
          {m.license && (
            <span className="tag" title={m.license.name}>
              {m.license.commercial_ok ? "Commercial use ok" : "Non-commercial"}
            </span>
          )}
        </div>
        <div className="model-actions">
          {m.installed ? (
            <>
              <span className="status quiet">
                <Icon name="check" size={14} />
                Installed
              </span>
              <span className="divider" aria-hidden />
              <button className="icon" onClick={onSettings} aria-label={`${m.name} settings`} title="Settings">
                <Icon name="sliders" size={16} />
              </button>
              <button className="icon delete" onClick={onDelete} disabled={busy} aria-label={`Delete ${m.name}`} title="Delete">
                <Icon name="trash" size={16} />
              </button>
            </>
          ) : dl?.status === "downloading" ? (
            <span className="status quiet num">
              {dl.total ? Math.round((dl.done / dl.total) * 100) : 0}%
              <button className="icon" aria-label={`Cancel ${m.name} download`} title="Cancel" onClick={() => act(m.id, "DELETE")}>
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
      <div className="model-body">
        <div className="model-meta">
          <span className="muted small num">
            {m.reason.replace(/\.$/, "")} · {gb(m.bytes)} on disk
          </span>
          {(m.speed_note || m.s_per_image) && (
            <span className="muted small num">
              {[m.speed_note, m.s_per_image && `${m.s_per_image.toFixed(1)} s per image, measured`].filter(Boolean).join(" · ")}
            </span>
          )}
          {dl?.status === "failed" && dl.error && <span className="error small">{dl.error}</span>}
        </div>
        <div className="fit">
          {budget > 0 && <FitGauge fit={m.fit} need={m.min_vram_gb} budget={budget} />}
          <FitLabel fit={m.fit}>{budget > 0 && ` · needs ${m.min_vram_gb.toFixed(1)} of ${budget.toFixed(1)} GB`}</FitLabel>
        </div>
      </div>
    </div>
  );
}

// Mirrors ModelSettings in host/app/catalog.py and _view() in host/app/model_settings.py.
type Values = { width?: number; height?: number; steps?: number; cfg?: number; negative?: string };
type SettingsView = {
  settings: Values;
  defaults: { width: number; height: number; steps: number | null; cfg: number | null; negative: string };
  limits: { steps: [number, number] | null; cfg: [number, number] | null; multiple_of: number; min_side: number; supports_negative: boolean };
};
const FIELDS = ["width", "height", "steps", "cfg", "negative"] as const;

/** The user's own defaults for one model; a request, style or quality choice still wins. */
function ModelSettings({ model, onClose }: { model: LocalModel; onClose: () => void }) {
  const [view, setView] = useState<SettingsView | null>(null);
  const [draft, setDraft] = useState<Record<(typeof FIELDS)[number], string>>({ width: "", height: "", steps: "", cfg: "", negative: "" });
  const [error, setError] = useState<string | null>(null);
  const url = `/api/v1/models/${model.id}/settings`;

  const load = (v: SettingsView) => {
    setView(v);
    setDraft(Object.fromEntries(FIELDS.map((f) => [f, String(v.settings[f] ?? "")])) as typeof draft);
  };
  useEffect(() => {
    api<SettingsView>(url).then(load, (e) => setError(e.message));
  }, [url]);

  const put = (body: Values) =>
    api<SettingsView>(url, { method: "PUT", body: JSON.stringify(body) }).then(
      (v) => (load(v), setError(null), true),
      (e) => (setError(e.message), false),
    );
  const save = () => {
    const body: Values = {};
    for (const f of FIELDS) {
      const v = draft[f].trim();
      if (v) (body as Record<string, string | number>)[f] = f === "negative" ? v : Number(v);
    }
    put(body).then((ok) => ok && onClose());
  };
  // Enter would submit the dialog's form, which closes it without saving.
  const enter = (e: KeyboardEvent) => {
    if (e.key === "Enter" && !(e.target instanceof HTMLTextAreaElement)) {
      e.preventDefault();
      save();
    }
  };
  const field = (f: (typeof FIELDS)[number]) => ({
    value: draft[f],
    onChange: (e: { target: { value: string } }) => setDraft({ ...draft, [f]: e.target.value }),
  });
  const lim = view?.limits;
  const def = view?.defaults;

  return (
    <Modal title={`${model.name} settings`} onClose={onClose}>
      <p className="small">Used when an image doesn't ask for something else. Leave a field empty for the model's default.</p>
      {view && lim && def && (
        <div className="form" onKeyDown={enter}>
          <div className="size-pair">
            <label>
              Width
              <input type="number" inputMode="numeric" min={lim.min_side} step={lim.multiple_of} placeholder={String(def.width)} {...field("width")} />
            </label>
            <label>
              Height
              <input type="number" inputMode="numeric" min={lim.min_side} step={lim.multiple_of} placeholder={String(def.height)} {...field("height")} />
            </label>
          </div>
          <span className="help">Both or neither, in multiples of {lim.multiple_of}.</span>
          {lim.steps && (
            <label>
              Steps
              <input type="number" inputMode="numeric" min={lim.steps[0]} max={lim.steps[1]} placeholder={String(def.steps ?? "")} {...field("steps")} />
              <span className="help">
                {lim.steps[0]} to {lim.steps[1]}. More steps: more detail, slower.
              </span>
            </label>
          )}
          {lim.cfg && (
            <label>
              CFG
              <input type="number" inputMode="decimal" min={lim.cfg[0]} max={lim.cfg[1]} step={0.1} placeholder={String(def.cfg ?? "")} {...field("cfg")} />
              <span className="help">
                {lim.cfg[0]} to {lim.cfg[1]}. How closely it follows the prompt.
              </span>
            </label>
          )}
          {lim.supports_negative && (
            <label>
              Negative prompt
              <textarea rows={2} maxLength={4000} placeholder={def.negative} {...field("negative")} />
            </label>
          )}
        </div>
      )}
      {error && <Alert>{error}</Alert>}
      <div className="actions">
        <button type="button" className="ghost" disabled={!view} onClick={() => put({})}>
          Reset to defaults
        </button>
        <button value="cancel">Cancel</button>
        <button type="button" className="primary" disabled={!view} onClick={save}>
          Save
        </button>
      </div>
    </Modal>
  );
}

/** What this computer can give models: the card at the top of Models. */
function ThisComputer({ hw, memory, system }: { hw: Hardware; memory: Memory; system: System | null }) {
  const gpu = hw.gpus?.[0];
  const { total_gb: total, models_gb: models, headroom_gb: headroom, system_gb: other } = memory;
  const kind = memory.unified ? (hw.backend === "mps" ? "unified memory" : "memory") : "graphics memory";
  const sub = [
    total > 0 && `${total.toFixed(0)} GB ${kind}`,
    hw.gpu_cores && `${hw.gpu_cores} GPU cores`,
    !memory.unified && `${hw.ram_gb.toFixed(0)} GB system memory`,
    hw.backend === "cpu" ? "no supported GPU, so images run on the processor" : hw.backend === "mps" ? "runs on Metal (MPS)" : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const segments: [string, string, number][] = [
    ["on", "Memory for models", models],
    ["mid", "Safety headroom", headroom],
    ["", memory.unified ? "OS + other apps" : "Kept free", other],
  ];
  const shown = segments.filter(([, , gbs]) => gbs > 0.05);
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
            {models.toFixed(1)}
            <small> GB</small>
          </span>
        </div>
      </div>

      {total > 0 && models > 0 && (
        <div>
          <div className="stackbar" role="img" aria-label={shown.map(([, label, gbs]) => `${label} ${gbs.toFixed(1)} GB`).join(", ")}>
            {shown.map(([cls, label, gbs]) => (
              <span key={label} className={`seg ${cls}`} style={{ flexGrow: gbs }} />
            ))}
          </div>
          <ul className="legend num">
            {shown.map(([cls, label, gbs]) => (
              <li key={label}>
                <span className={`key ${cls}`} aria-hidden="true" />
                {label} <strong>{gbs.toFixed(1)} GB</strong>
              </li>
            ))}
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
        <span className="tag num">Free in models folder {hw.disk_free_gb.toFixed(0)} GB</span>
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
