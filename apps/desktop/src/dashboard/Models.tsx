import { FormEvent, KeyboardEvent, useEffect, useState } from "react";
import { api, gb, Hardware } from "./api";
import { Alert, Fit, FitGauge, FitLabel, Icon, IconName, Modal, PageHeader, Progress, Tabs, useConfirm } from "./ui";

// Mirror recommend_one(), add_tiers(), memory_split() and Downloads.view() in host/app/local_models.py.
type Download = { status: "downloading" | "done" | "failed" | "cancelled"; done: number; total: number; verifying: boolean; error: string | null };
type Tier = "best" | "better" | "faster" | "other" | "comfyui";
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
  cost: number; // speed_cost(): only the ratio between two models means anything
  download: Download | null;
  description: string;
  categories: Category[];
  abilities: { steps: number | null; negative: boolean; seed: boolean; custom_size: boolean; max_pixels: number | null };
};
type Category = "photo" | "art" | "text";
type Memory = { unified: boolean; total_gb: number; models_gb: number; headroom_gb: number; system_gb: number };

type Section = [Tier, string, string, IconName];
const TIERS: Section[] = [
  ["best", "Best for this computer", "Tested in Glint Studio. Runs well here, best quality first", "star"],
  ["better", "Better quality, slower", "Too big to run fully on the GPU, so each image takes longer", "bulb"],
  ["faster", "Faster, lighter", "Quicker images, a step down in quality", "bolt"],
  ["other", "Also runs here", "Slower than the best pick, without better quality", "layers"],
];
// Templates that come with ComfyUI (host/app/comfy_templates.py), below the app's own models.
const COMFYUI: Section = ["comfyui", "From ComfyUI's templates", "They run here, but aren't tested in Glint Studio yet", "box"];
const FILTERS = [
  ["all", "All", "grid"],
  ["photo", "Photo", "camera"],
  ["art", "Anime & art", "palette"],
  ["text", "Text in images", "text"],
  ["fast", "Few steps", "bolt"],
] as const satisfies readonly (readonly [string, string, IconName])[];
type Filter = (typeof FILTERS)[number][0];
const FAST_STEPS = 8;
const fast = (m: LocalModel) => m.abilities.steps !== null && m.abilities.steps <= FAST_STEPS;
const matches = (m: LocalModel, f: Filter) => f === "all" || (f === "fast" ? fast(m) : m.categories.includes(f));
const runsHere = (m: LocalModel) => m.fit !== "wont_fit" || m.installed;
const SPEED = ["Slowest", "Slower", "Medium", "Faster", "Fastest"];

/** 1 to 5 by estimated time per image, against the other models that run here. Log scale: costs span
 * orders of magnitude. A model that spills out of GPU memory is the slowest whatever its size. */
function speedLevels(models: LocalModel[]): Map<string, number> {
  const runs = models.filter(runsHere);
  const logs = runs.map((m) => Math.log(Math.max(m.cost, 1)));
  const lo = Math.min(...logs);
  const hi = Math.max(...logs);
  return new Map(runs.map((m, i) => [m.id, m.fit === "slow" ? 1 : hi - lo < 0.1 ? 3 : 1 + Math.round((4 * (hi - logs[i])) / (hi - lo))]));
}

/** `tab` comes from the address (#models/installed), so the header menu can open either view. */
export default function Models({ tab }: { tab: "discover" | "installed" }) {
  const [models, setModels] = useState<LocalModel[] | null>(null);
  const [hw, setHw] = useState<Hardware | null>(null);
  const [memory, setMemory] = useState<Memory | null>(null);
  const [unlock, setUnlock] = useState<string[]>([]);
  const [editing, setEditing] = useState<LocalModel | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
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
    window.addEventListener("focus", refresh); // models found after the page opened (ComfyUI's templates, online catalog)
    return () => window.removeEventListener("focus", refresh);
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
  const speed = speedLevels(models ?? []);
  const props = (m: LocalModel): RowProps => ({ m, budget, busy, act, speed: speed.get(m.id), onSettings: () => setEditing(m), onDelete: () => remove(m) });
  const row = (m: LocalModel) => <ModelRow key={m.id} {...props(m)} />;
  const shown = models?.filter((m) => matches(m, filter)) ?? [];
  const wontRun = shown.filter((m) => !runsHere(m));
  const tierSection = ([tier, title, cap, icon]: Section) => {
    const rows = shown.filter((m) => m.tier === tier && runsHere(m));
    return (
      rows.length > 0 && (
        <section key={tier} className="tier" aria-labelledby={`tier-${tier}`}>
          <SectionHead id={`tier-${tier}`} icon={icon} title={title} cap={cap} />
          {tier === "best" ? (
            <div className="pick-grid">
              {rows.map((m, i) => (
                <PickCard key={m.id} first={i === 0 && filter === "all"} {...props(m)} />
              ))}
            </div>
          ) : (
            <div className="card list-card">{rows.map(row)}</div>
          )}
        </section>
      )
    );
  };
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
      {tab === "discover" && hw && memory && <ThisComputer hw={hw} memory={memory} />}

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
          <fieldset className="filter-bar">
            <legend className="sr-only">Good for</legend>
            <span className="filter-label" aria-hidden="true">
              Good for
            </span>
            <div className="chip-row">
              {FILTERS.map(([id, label, icon]) => {
                const count = models.filter((m) => matches(m, id)).length;
                return (
                  (count > 0 || id === filter) && (
                    <label key={id} className="chip">
                      <input type="radio" name="model-filter" checked={filter === id} onChange={() => setFilter(id)} />
                      <Icon name={icon} size={16} />
                      {label}
                      <span className="count num">{count}</span>
                    </label>
                  )
                );
              })}
            </div>
          </fieldset>
          {TIERS.map(tierSection)}
          {unlockNames.length > 0 && (
            <section className="card unlock" aria-labelledby="unlock-title">
              <SectionHead id="unlock-title" icon="unlock" title="Unlock bigger models" cap="Advanced" />
              <p className="small">
                {unlockNames.join(", ")} would run fully on the GPU if models could use more memory. Raise the memory guard in{" "}
                <a href="#settings/engine">Settings → Engine</a>. Less memory is then left for other apps.
              </p>
            </section>
          )}
          {tierSection(COMFYUI)}
          {wontRun.length > 0 && (
            <section className="card list-card">
              <details className="wont">
                <summary className="card-head">
                  <div>
                    <h2>
                      Won't run on this computer <span className="num muted">({wontRun.length})</span>
                    </h2>
                    <span className="cap">Not enough memory or disk. Each row says what it needs</span>
                  </div>
                  <Icon name="chevron" size={16} />
                </summary>
                {wontRun.map(row)}
              </details>
            </section>
          )}
        </>
      )}
      {tab === "discover" && models && models.length > 0 && (
        <p className="cap">Speed compares the models on this page, estimated from their size, steps and image size. Run Bench to measure one.</p>
      )}
      {tab === "discover" && <CatalogLine onAdded={refresh} />}
      {tab === "installed" && <ComfyFolders onFolders={refresh} />}
      {editing && <ModelSettings model={editing} onClose={() => setEditing(null)} />}
      {confirm.dialog}
    </>
  );
}

// Mirrors CatalogRefresh.view() in host/app/catalog_refresh.py.
type CatalogStatus = {
  available: boolean;
  enabled: boolean;
  checked_at: number | null;
  added: string[];
  needs_update: string[];
  error: string | null;
};

/** The online catalog: models added since this release, checked once at each start. */
function CatalogLine({ onAdded }: { onAdded: () => void }) {
  const [status, setStatus] = useState<CatalogStatus | null>(null);
  const [checking, setChecking] = useState(false);
  useEffect(() => {
    api<CatalogStatus>("/api/v1/catalog").then(setStatus, () => setStatus(null));
  }, []);
  if (!status?.available) return null;

  const check = () => {
    setChecking(true);
    api<CatalogStatus>("/api/v1/catalog/refresh", { method: "POST" })
      .then((s) => (setStatus(s), onAdded()), (e) => setStatus({ ...status, error: e.message }))
      .finally(() => setChecking(false));
  };
  const at = status.checked_at && new Date(status.checked_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const n = status.added.length;
  const later = status.needs_update.length;
  return (
    <p className="cap catalog-line" role="status">
      <span>
        {checking
          ? "Checking for new models…"
          : status.error
            ? `Couldn't check for new models: ${status.error}`
            : at
              ? `Checked for new models at ${at}.` + (n ? ` ${n} added since this version.` : "")
              : status.enabled
                ? "Not checked for new models yet."
                : "Checking for new models at start is off."}
        {!checking && later > 0 && ` ${later} more need an app update.`}
      </span>
      <button type="button" className="ghost small" onClick={check} disabled={checking}>
        Check now
      </button>
    </p>
  );
}

type RowProps = {
  m: LocalModel;
  budget: number;
  busy: boolean;
  act: (id: string, method: "POST" | "DELETE") => void;
  speed: number | undefined; // speedLevels(); none for a model that won't run
  onSettings: () => void;
  onDelete: () => void;
};

function SectionHead({ id, icon, title, cap }: { id: string; icon: IconName; title: string; cap: string }) {
  return (
    <div className="section-head">
      <span className="section-icon" aria-hidden="true">
        <Icon name={icon} size={16} />
      </span>
      <div>
        <h2 id={id}>{title}</h2>
        <span className="cap">{cap}</span>
      </div>
    </div>
  );
}

function ModelRow({ m, budget, busy, act, speed, onSettings, onDelete }: RowProps) {
  const dl = m.download;
  return (
    <div className={m.fit === "wont_fit" && !m.installed ? "model-row off" : "model-row"}>
      <div className="model-head">
        <div className="model-name">
          <strong>{m.name}</strong>
          <Tags m={m} />
        </div>
        <div className="model-actions">
          <ModelActions m={m} busy={busy} act={act} onSettings={onSettings} onDelete={onDelete} />
        </div>
      </div>
      <div className="model-body">
        <div className="model-meta">
          {m.description && <span className="small">{m.description}</span>}
          <Abilities m={m} />
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
        <FitBlock m={m} budget={budget} speed={speed} />
      </div>
    </div>
  );
}

/** A top pick, as a card: what it's good for, how fast and how much memory, and one button. */
function PickCard({ m, first, budget, busy, act, speed, onSettings, onDelete }: RowProps & { first: boolean }) {
  const dl = m.download;
  return (
    <article className={first ? "card pick first" : "card pick"} aria-labelledby={`pick-${m.id}`}>
      {first && (
        <span className="best-badge">
          <Icon name="star" size={14} />
          Best match
        </span>
      )}
      <div className="pick-main">
        <div className="pick-title">
          <h3 id={`pick-${m.id}`}>{m.name}</h3>
          {m.description && <span className="small muted">{m.description}</span>}
        </div>
        <div className="model-name">
          <Tags m={m} />
        </div>
        <Abilities m={m} />
        {m.fit !== "great" && <span className="muted small">{m.reason}</span>}
        {dl?.status === "failed" && dl.error && <span className="error small">{dl.error}</span>}
      </div>
      <div className="pick-side">
        <FitBlock m={m} budget={budget} speed={speed} />
        <div className="pick-foot">
          <span className="muted small num">{m.installed ? `${gb(m.bytes)} on disk` : `${gb(m.bytes)} download`}</span>
          <div className="model-actions">
            <ModelActions m={m} busy={busy} act={act} onSettings={onSettings} onDelete={onDelete} />
          </div>
        </div>
      </div>
    </article>
  );
}

function Tags({ m }: { m: LocalModel }) {
  return (
    <>
      {m.tier === "comfyui" && <span className="tag">ComfyUI template</span>}
      <span className="tag">{m.precision}</span>
      {m.license && (
        <span className="tag" title={m.license.name}>
          {m.license.commercial_ok ? "Commercial use ok" : "Non-commercial"}
        </span>
      )}
    </>
  );
}

function Abilities({ m }: { m: LocalModel }) {
  return (
    <ul className="abilities" aria-label={`What ${m.name} can do`}>
      {abilities(m).map((a) => (
        <li key={a} className="tag">
          {a}
        </li>
      ))}
    </ul>
  );
}

/** Speed dots, then the memory gauge and whether it fits. */
function FitBlock({ m, budget, speed }: { m: LocalModel; budget: number; speed: number | undefined }) {
  return (
    <div className="fit">
      {speed && (
        <span className="speed">
          <span className="cap">
            Speed · <strong>{SPEED[speed - 1]}</strong>
          </span>
          <span className="dots" role="img" aria-label={`Speed ${speed} of 5`}>
            {[1, 2, 3, 4, 5].map((i) => (
              <i key={i} className={i <= speed ? "on" : undefined} />
            ))}
          </span>
        </span>
      )}
      {budget > 0 && <FitGauge fit={m.fit} need={m.min_vram_gb} budget={budget} />}
      <FitLabel fit={m.fit}>{budget > 0 && ` · needs ${m.min_vram_gb.toFixed(1)} of ${budget.toFixed(1)} GB`}</FitLabel>
    </div>
  );
}

function ModelActions({ m, busy, act, onSettings, onDelete }: Omit<RowProps, "budget" | "speed">) {
  const dl = m.download;
  return m.installed ? (
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
  );
}

/** Short, plain words for a row's abilities (recommend_one() in host/app/local_models.py). */
function abilities(m: LocalModel): string[] {
  const a = m.abilities;
  const mp = a.max_pixels && Math.round((a.max_pixels / 1e6) * 10) / 10;
  return [
    a.steps !== null && (fast(m) ? `Few steps · ${a.steps}` : `${a.steps} steps`),
    a.custom_size ? (mp ? `Any size up to ${mp} MP` : "Any size") : "Set sizes only",
    a.negative && "Negative prompt",
    a.seed && "Repeatable with a seed",
  ].filter((x): x is string => !!x);
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
function ThisComputer({ hw, memory }: { hw: Hardware; memory: Memory }) {
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

  return (
    <section className="card hw" aria-labelledby="hw-title">
      <div className="hw-top">
        <span className="hw-icon" aria-hidden="true">
          <Icon name="cpu" size={22} />
        </span>
        <div className="hw-id">
          <span className="hw-label">This computer</span>
          <h2 id="hw-title">{gpu?.name ?? hw.cpu ?? "Unknown processor"}</h2>
        </div>
        <div className="hw-budget num">
          <strong>{models.toFixed(1)} GB</strong> for models
        </div>
      </div>
      {total > 0 && models > 0 && (
        <div className="stackbar" role="img" aria-label={shown.map(([, label, gbs]) => `${label} ${gbs.toFixed(1)} GB`).join(", ")}>
          {shown.map(([cls, label, gbs]) => (
            <span key={label} className={`seg ${cls}`} style={{ flexGrow: gbs }} />
          ))}
        </div>
      )}
      <div className="hw-foot num">
        <ul className="legend">
          {shown.map(([cls, label, gbs]) => (
            <li key={label}>
              <span className={`key ${cls}`} aria-hidden="true" />
              {label} <strong>{gbs.toFixed(1)} GB</strong>
            </li>
          ))}
        </ul>
        <span className="muted">
          {sub}
          {sub && " · "}
          {hw.disk_free_gb.toFixed(0)} GB free on disk
        </span>
      </div>
      {hw.warnings?.map((w) => (
        <p key={w} className="cap">
          {w}
        </p>
      ))}
    </section>
  );
}

// Mirrors engine_settings() in host/app/dashboard.py.
type Folder = { path: string; models: number; exists: boolean };
type EngineSettings = { folders: Folder[]; found: Folder[] };

/** Model folders from a ComfyUI already on this computer. The model in memory lives in Settings › Engine. */
function ComfyFolders({ onFolders }: { onFolders: () => void }) {
  const [settings, setSettings] = useState<EngineSettings | null>(null);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<EngineSettings>("/api/v1/engine/settings").then(setSettings, (e) => setError(e.message));
  }, []);

  const save = (body: { folders: string[] }) => {
    setBusy(true);
    return api<EngineSettings>("/api/v1/engine/settings", { method: "PUT", body: JSON.stringify(body) })
      .then(
        (s) => {
          setSettings(s);
          setError(null);
          onFolders(); // linked models count as installed
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
  return (
    <section className="card" aria-labelledby="folders-title">
      <div className="card-head">
        <h2 id="folders-title">Models from your ComfyUI</h2>
        <span className="cap">Linking restarts the engine</span>
      </div>
      <p className="muted small">Already have ComfyUI? Link its models folder and Glint Studio uses those files instead of downloading them again.</p>
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
