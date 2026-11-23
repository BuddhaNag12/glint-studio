import { CSSProperties, FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { api, JobEvent, Model, onJobEvents, Quality, secs, Style } from "./api";
import { Alert, Icon, iconOr, Tabs, useConfirm } from "./ui";
import History, { HistoryItem } from "./History";
import StyleEditor, { BLANK, copyOf, Listed } from "./Styles";

// Each built-in style's tile, made with the model itself (src/dashboard/styles/<id>.jpg).
const SAMPLES = import.meta.glob("./styles/*.jpg", { eager: true, query: "?url", import: "default" }) as Record<string, string>;
const sampleOf = (id: string): string | undefined => SAMPLES[`./styles/${id}.jpg`];

const EXAMPLES = [
  "A lighthouse on a cliff at dusk, warm light in the windows",
  "A red fox curled up in fresh snow",
  "A cozy reading nook with rain on the window",
  "A tiny astronaut tending a garden on the moon",
];
const QUALITY: Record<Quality, string> = { fast: "Fast", standard: "Standard", best: "Best" };

type Body = {
  prompt: string;
  model: string;
  style?: string;
  size?: string;
  quality?: Quality;
  seed?: number;
  steps?: number;
  cfg?: number;
  negative_prompt?: string;
};
// GET /api/v1/jobs/{id}: jobs.view() plus seed and data once done.
type JobView = {
  id: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  queue_position: number;
  progress: number;
  elapsed_ms: number | null;
  steps?: number | null; // the steps it ran, for time per step
  error: { code: string; message: string } | null;
  seed?: number | null;
  data?: { b64_json?: string }[];
};
/** One prompt and the image it made. Finished turns are kept in this browser, grouped into chats. */
type Turn = Omit<JobView, "data"> & {
  chat?: string; // absent on turns kept before there were several chats: they make up one chat, FIRST
  body: Body;
  modelName: string;
  styleName: string;
  shape: string;
  w: number;
  h: number;
  created: number;
  image?: string; // base64 PNG
  preview?: string; // base64 JPEG while sampling; never stored
};

// The page scrolls, with the composer stuck to the bottom: the newest turn is at the very end.
const toEnd = (behavior: ScrollBehavior) => requestAnimationFrame(() => document.scrollingElement?.scrollTo({ top: 1e9, behavior }));
const finished = (r: { status: string }) => ["done", "failed", "cancelled"].includes(r.status);
const FIRST = "first";
const chatOf = (t: Turn) => t.chat ?? FIRST;
const newChat = () => Date.now().toString(36); // crypto.randomUUID needs https, and the phone page is plain http
const WIDE = "(min-width: 1200px)"; // room for the history beside the chat; narrower, it opens as a sheet

// Mirrors Profile in host/app/history.py: "" and null mean the default.
type Profile = {
  name: string;
  model: string;
  style: string;
  shape: string;
  quality: Quality | "";
  steps: number | null;
  cfg: number | null;
  seed: number | null;
  negative: string;
};

// shortcut: the chat lives in this browser's IndexedDB with no cap (a 1024² PNG is ~1.5 MB); add paging or a
// limit if people keep hundreds of images here.
const store = (mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest | void) =>
  new Promise<unknown>((ok, fail) => {
    const open = indexedDB.open("glint-chat", 1);
    open.onupgradeneeded = () => open.result.createObjectStore("turns", { keyPath: "id" });
    open.onerror = () => fail(open.error);
    open.onsuccess = () => {
      const tx = open.result.transaction("turns", mode);
      const req = fn(tx.objectStore("turns"));
      tx.oncomplete = () => ok(req?.result);
      tx.onerror = () => fail(tx.error);
    };
  });

/** A chat: each message is a full prompt, each reply the image it made. `phone` is the paired-phone page. */
export default function Generate({ phone }: { phone?: boolean }) {
  const [models, setModels] = useState<Model[]>([]);
  const [styles, setStyles] = useState<Listed[]>([]);
  const [modelId, setModelId] = useState("");
  const [styleId, setStyleId] = useState("");
  const [shape, setShape] = useState(""); // "": the style's shape, else the model's default
  const [quality, setQuality] = useState<Quality | "">("");
  const [seed, setSeed] = useState("");
  const [steps, setSteps] = useState("");
  const [cfg, setCfg] = useState("");
  const [negative, setNegative] = useState("");
  const [prompt, setPrompt] = useState("");
  const [panel, setPanel] = useState<"" | "style" | "more">(""); // the drawer open above the prompt
  const [turns, setTurns] = useState<Turn[]>([]);
  const [editing, setEditing] = useState<{ style: Style; fresh: boolean } | null>(null);
  const [full, setFull] = useState<Turn | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wide, setWide] = useState(() => matchMedia(WIDE).matches);
  const [sheet, setSheet] = useState(false); // the history, on narrow screens
  const [chatId, setChatId] = useState(newChat);
  const [side, setSide] = useState<"chats" | "images">("chats");
  const live = useRef(turns);
  live.current = turns;
  const kept = useRef(new Set<string>());
  const box = useRef<HTMLTextAreaElement>(null);
  const composer = useRef<HTMLDivElement>(null);

  // An open drawer closes like a menu: Esc, or a press outside the composer (not while the style editor is open).
  useEffect(() => {
    if (!panel) return;
    const close = (e: Event) => {
      if (document.querySelector("dialog[open]")) return;
      if (e.type === "keydown" ? (e as globalThis.KeyboardEvent).key === "Escape" : !composer.current?.contains(e.target as Node)) setPanel("");
    };
    document.addEventListener("keydown", close);
    document.addEventListener("pointerdown", close);
    return () => (document.removeEventListener("keydown", close), document.removeEventListener("pointerdown", close));
  }, [panel]);
  const confirm = useConfirm();
  useEffect(() => {
    const m = matchMedia(WIDE);
    const on = () => setWide(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);

  const loadStyles = () =>
    api<{ styles: Listed[] }>("/api/v1/styles").then(
      (r) => (setStyles(r.styles), r.styles),
      (e) => (setError(e.message), [] as Listed[]),
    );
  useEffect(() => {
    api<{ models: Model[] }>("/api/v1/models").then((r) => {
      setModels(r.models);
      setModelId((id) => id || r.models[0]?.id || "");
    }, (e) => setError(e.message));
    loadStyles();
    store("readonly", (s) => s.getAll()).then((all) => {
      // Turns kept before the merge fix hold seconds; any real ms time is past 1e12.
      const old = (all as Turn[]).map((t) => (t.created < 1e12 ? { ...t, created: t.created * 1000 } : t)).sort((a, b) => a.created - b.created);
      old.filter(finished).forEach((t) => kept.current.add(t.id));
      setTurns((now) => [...old, ...now.filter((t) => !old.some((o) => o.id === t.id))]);
      // Reopen the latest chat, unless a message went out before the kept ones loaded.
      if (old.length) setChatId((id) => (live.current.some((t) => chatOf(t) === id) ? id : chatOf(old[old.length - 1])));
      // Jobs still going when the page was left: catch up, or mark them lost if the engine restarted since.
      for (const t of old.filter((t) => !finished(t)))
        api<JobView>(`/api/v1/jobs/${t.id}`).then(
          ({ data, ...v }) => merge(t.id, { ...v, image: data?.[0]?.b64_json }),
          () => merge(t.id, { status: "failed", error: { code: "lost", message: "This one was lost when the engine restarted." } }),
        );
      toEnd("auto");
    }, () => {}); // no IndexedDB (a private window): the chat just isn't kept
  }, []);

  // Keep each turn once it has its final state; the image arrives a moment after the "done" event.
  useEffect(() => {
    for (const t of turns)
      if (finished(t) && (t.status !== "done" || t.image) && !kept.current.has(t.id)) {
        kept.current.add(t.id);
        const { preview: _p, ...rest } = t;
        store("readwrite", (s) => s.put(rest)).catch(() => {});
      }
  }, [turns]);

  // A finished turn never goes back to unfinished, whichever of the event and the fetch lands last.
  // The job's own `created` is in seconds; the turn keeps its ms one.
  const merge = (id: string, v: Partial<Turn>) =>
    setTurns((all) => all.map((t) => (t.id !== id || (finished(t) && !finished({ status: v.status ?? t.status })) ? t : { ...t, ...v, created: t.created })));
  const refresh = (id: string) =>
    api<JobView>(`/api/v1/jobs/${id}`).then(({ data, ...v }) => merge(id, { ...v, image: data?.[0]?.b64_json }), () => {});

  // Events carry progress and previews; the image and seed come from the job once it's done. After a
  // reconnect, catch up on every unfinished turn, since its events may have been missed.
  useEffect(
    () =>
      onJobEvents(
        ({ event, preview, ...e }: JobEvent) => {
          merge(e.id, event === "preview" ? { ...e, preview } : e);
          if (finished(e)) refresh(e.id);
        },
        () => live.current.filter((t) => !finished(t)).forEach((t) => refresh(t.id)),
      ),
    [],
  );

  const model = models.find((m) => m.id === modelId);
  const usable = styles.filter((s) => !s.models || s.models.includes(modelId));
  const style = usable.find((s) => s.id === styleId);
  const qualities = (Object.keys(QUALITY) as Quality[]).filter((q) => model?.quality[q]);
  const preset = quality ? model?.quality[quality] : undefined;
  const [minSteps, maxSteps] = model?.limits.steps ?? [];
  const [minCfg, maxCfg] = model?.limits.cfg ?? [];
  const pickModel = (id: string) => {
    setModelId(id);
    setShape("");
    setQuality("");
    setSteps("");
    setCfg("");
  };

  const send = (body: Body, m: Model, styleName: string, shapeName: string) => {
    // The frame takes the image's shape before the image exists: the shape asked for, the style's, or the model's.
    const named = shapeName || String(styles.find((s) => s.id === body.style)?.overrides.shape ?? "") || m.defaults.shape;
    const [w, h] = m.limits.sizes[named] ?? [1, 1];
    setError(null);
    api<JobView>("/api/v1/jobs", { method: "POST", body: JSON.stringify(body) }).then(
      (job) => {
        const turn: Turn = { ...job, chat: chatId, progress: 0, elapsed_ms: null, error: null, body, modelName: m.name, styleName, shape: shapeName, w, h, created: Date.now() };
        setTurns((all) => [...all, turn]);
        store("readwrite", (s) => s.put(turn)).catch(() => {}); // kept now too, so leaving the page doesn't lose it
        toEnd("smooth");
        refresh(job.id); // in case it finished before it was in the list
      },
      (e) => setError(e.message),
    );
  };

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (!model || !prompt.trim()) return;
    const size = shape && model.limits.sizes[shape];
    const num = (v: string) => (v === "" ? undefined : Number(v));
    send(
      {
        prompt: prompt.trim(),
        model: modelId,
        style: styleId || undefined,
        size: size ? `${size[0]}x${size[1]}` : undefined,
        quality: quality || undefined,
        seed: num(seed),
        steps: num(steps),
        cfg: num(cfg),
        negative_prompt: negative.trim() || undefined,
      },
      model,
      style?.name ?? "",
      shape,
    );
    setPrompt("");
  };
  // Desktop: Enter sends, Shift+Enter is a new line. On a phone the keyboard's return is a new line.
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!phone && e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };
  // The box grows with the prompt up to its max height, then scrolls.
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [prompt]);

  const again = (t: Turn) => {
    const m = models.find((x) => x.id === t.body.model);
    if (!m) return setError(`${t.modelName} isn't available any more.`);
    send({ ...t.body, seed: undefined }, m, t.styleName, t.shape);
  };
  const edit = (t: Turn) => {
    setPrompt(t.body.prompt);
    if (models.some((m) => m.id === t.body.model)) setModelId(t.body.model);
    setStyleId(t.body.style ?? "");
    setShape(t.shape);
    box.current?.focus();
  };

  // A past image from the host's history, as a turn, so the chat's own actions work on it.
  const fromHistory = (h: HistoryItem, image?: string): Turn => {
    const m = models.find((x) => x.id === h.model);
    const sized = Object.entries(m?.limits.sizes ?? {}).find(([, [w, ht]]) => w === h.width && ht === h.height);
    return {
      id: h.id, status: "done", queue_position: 0, progress: 1, elapsed_ms: h.elapsed_ms, steps: h.steps, error: null, seed: h.seed,
      body: { prompt: h.prompt, model: h.model, style: h.style ?? undefined, steps: h.steps ?? undefined, cfg: h.cfg ?? undefined, negative_prompt: h.negative ?? undefined },
      modelName: m?.name ?? h.model, styleName: styles.find((s) => s.id === h.style)?.name ?? h.style ?? "", shape: sized?.[0] ?? "",
      w: h.width ?? 1, h: h.height ?? 1, created: h.created * 1000, image,
    };
  };
  const reuse = (h: HistoryItem) => {
    const t = fromHistory(h);
    edit(t);
    setSteps(t.body.steps?.toString() ?? "");
    setCfg(t.body.cfg?.toString() ?? "");
    setNegative(t.body.negative_prompt ?? "");
    setSeed(h.seed?.toString() ?? "");
    setSheet(false);
  };
  const made = turns.filter((t) => t.status === "done").length; // refreshes the history as images finish
  const history = (
    <History
      refresh={made}
      names={(h) => { const t = fromHistory(h); return { model: t.modelName, style: t.styleName }; }}
      onOpen={(h, image) => setFull(fromHistory(h, image))}
      onUse={reuse}
      onAgain={(h) => (setSheet(false), again(fromHistory(h)))}
    />
  );

  const num = (v: string) => (v === "" ? null : Number(v));
  const current = { model: modelId, style: styleId, shape, quality, steps: num(steps), cfg: num(cfg), seed: num(seed), negative };
  const apply = (p: Profile) => {
    const m = models.find((x) => x.id === p.model) ?? model;
    if (m && m.id !== modelId) pickModel(m.id);
    setStyleId(p.style);
    setShape(m?.limits.sizes[p.shape] ? p.shape : "");
    setQuality(p.quality && m?.quality[p.quality] ? p.quality : "");
    setSteps(p.steps?.toString() ?? "");
    setCfg(p.cfg?.toString() ?? "");
    setSeed(p.seed?.toString() ?? "");
    setNegative(p.negative);
    if (p.model && m?.id !== p.model) setError(`${p.model} isn't available, so the profile was applied to ${m?.name}.`);
  };
  const cancel = (id: string) => api(`/api/v1/jobs/${id}`, { method: "DELETE" }).catch((e) => setError(e.message));
  const shown = turns.filter((t) => chatOf(t) === chatId);
  const startChat = () => (setChatId(newChat()), box.current?.focus());
  const openChat = (id: string) => (setChatId(id), setSheet(false), toEnd("auto"));
  // The images stay in the host's history (the Images tab); only the chat goes.
  const forget = (gone: Turn[]) => {
    store("readwrite", (s) => gone.forEach((t) => s.delete(t.id))).catch(() => {});
    gone.forEach((t) => kept.current.delete(t.id));
    setTurns((all) => all.filter((t) => !gone.includes(t)));
  };
  const deleteChat = (id: string, title: string) =>
    confirm.ask({ title: "Delete this chat?", body: `"${title}" goes from this browser. Its images stay under Images.`, action: "Delete chat" }).then((ok) => {
      if (!ok) return;
      forget(turns.filter((t) => chatOf(t) === id));
      if (id === chatId) setChatId(newChat());
    });
  const deleteAll = () =>
    confirm.ask({ title: "Delete all chats?", body: "Every chat goes from this browser. The images stay under Images.", action: "Delete all" }).then((ok) => {
      if (!ok) return;
      forget(turns.filter(finished));
      setChatId(newChat());
    });
  const past = (
    <>
      <Tabs label="History" value={side} onChange={setSide} options={[["chats", "Chats"], ["images", "Images"]]} />
      {side === "chats" ? <Chats turns={turns} current={chatId} onOpen={openChat} onDelete={deleteChat} onDeleteAll={deleteAll} /> : history}
    </>
  );

  return (
    <div className={wide ? "gen wide" : "gen"}>
      {wide && (
        <aside className="history-side" aria-label="History">
          {past}
        </aside>
      )}
      <div className={phone ? "chat phone-chat" : "chat"}>
        <h1 className="sr-only">Generate</h1>
        {confirm.dialog}
        {(shown.length > 0 || !wide) && (
          <div className="chat-top">
            {!wide && (
              <button type="button" className="ghost small" aria-haspopup="dialog" onClick={() => setSheet(true)}>
                <Icon name="layers" size={16} />
                History
              </button>
            )}
            {shown.length > 0 && (
              <button type="button" className="ghost small" onClick={startChat}>
                <Icon name="newchat" size={16} />
                New chat
              </button>
            )}
          </div>
        )}

        <div className="chat-log" aria-live="polite">
          {shown.length === 0 ? (
            <div className="hello">
              <p className="hello-title">What should we make?</p>
              <p className="muted">Describe an image. Each message makes a new one, on this computer.</p>
              <div className="examples">
                {EXAMPLES.map((x) => (
                  <button key={x} type="button" className="example" onClick={() => (setPrompt(x), box.current?.focus())}>
                    {x}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            shown.map((t) => <TurnView key={t.id} t={t} onCancel={cancel} onAgain={again} onEdit={edit} onFull={setFull} />)
          )}
        </div>

        <form className="dock" onSubmit={submit}>
          {error && <Alert>{error}</Alert>}
          <div className="composer" ref={composer}>
            <div className={panel === "style" ? "drawer open" : "drawer"} inert={panel !== "style"}>
              <div>
                <StyleTiles
                  styles={usable}
                  value={styleId}
                  onChange={(id) => (setStyleId(id), setPanel(""), phone || box.current?.focus())}
                  onEdit={(s) => setEditing({ style: s, fresh: false })}
                  onNew={() => setEditing({ style: copyOf(BLANK, "New style", styles), fresh: true })}
                />
              </div>
            </div>
            <div className={panel === "more" ? "drawer open" : "drawer"} inert={panel !== "more"}>
              <div>
                {model && (
                  <div className="more-panel form">
                    {!phone && <Profiles current={current} onApply={apply} />}
                    <div className="two">
                      {models.length > 1 && (
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
                      )}
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
                      {model.limits.supports_seed && (
                        <label>
                          Variation number
                          <input type="number" min={0} value={seed} placeholder="Random" onChange={(e) => setSeed(e.target.value)} />
                        </label>
                      )}
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
                        Negative prompt
                        <textarea value={negative} onChange={(e) => setNegative(e.target.value)} rows={2} maxLength={4000} />
                        <span className="help">Added to the model's and the style's, never replacing them</span>
                      </label>
                    )}
                  </div>
                )}
              </div>
            </div>
            <textarea
              ref={box}
              className="prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onKeyDown={onKey}
              rows={1}
              maxLength={4000}
              aria-label="Describe the image"
              placeholder={style ? `Describe the image · ${style.name}` : "Describe the image"}
            />
            <div className="composer-row">
              <button type="button" className={panel === "style" ? "pill on" : "pill"} aria-expanded={panel === "style"} aria-label={`Style: ${style?.name ?? "None"}`} onClick={() => setPanel(panel === "style" ? "" : "style")}>
                {style && thumbOf(style) ? <img className="pill-art" src={thumbOf(style)} alt="" /> : <Icon name={style ? iconOr(style.icon) : "palette"} size={16} />}
                <span className="pill-name">{style?.name ?? "Style"}</span>
                <Icon name="chevron" size={14} className="chev" />
              </button>
              {model && (
                <label className="pill">
                  <Icon name="square" size={16} />
                  <span className="sr-only">Shape</span>
                  <select value={shape} onChange={(e) => setShape(e.target.value)}>
                    <option value="">{style?.overrides.shape ? `Style's (${style.overrides.shape})` : "Default shape"}</option>
                    {Object.entries(model.limits.sizes).map(([name, [w, h]]) => (
                      <option key={name} value={name}>
                        {name[0].toUpperCase() + name.slice(1)} · {w}×{h}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <button type="button" className={panel === "more" ? "pill on" : "pill"} aria-expanded={panel === "more"} aria-label="More settings" onClick={() => setPanel(panel === "more" ? "" : "more")}>
                <Icon name="sliders" size={16} />
                <span className="pill-name">More</span>
              </button>
              <button type="submit" className="send" aria-label="Generate" disabled={!model || !prompt.trim()}>
                <Icon name="up" size={20} />
              </button>
            </div>
          </div>
        </form>

        {editing && (
          <StyleEditor
            start={editing.style}
            fresh={editing.fresh}
            styles={styles}
            reload={loadStyles}
            onSaved={setStyleId}
            onClose={() => setEditing(null)}
          />
        )}
        {full?.image && <Lightbox t={full} onClose={() => setFull(null)} />}
      </div>
      {sheet && <HistorySheet onClose={() => setSheet(false)}>{past}</HistorySheet>}
    </div>
  );
}

// Sample art only for a built-in style as shipped; an edited one may not look like it any more.
const thumbOf = (s: Listed) => (s.builtin && !s.edited ? sampleOf(s.id) : undefined);

/** The grid of style tiles: None, every style, then "+". Hover a tile for its pencil, or long-press it on a phone. */
function StyleTiles({
  styles,
  value,
  onChange,
  onEdit,
  onNew,
}: {
  styles: Listed[];
  value: string;
  onChange: (id: string) => void;
  onEdit: (s: Listed) => void;
  onNew: () => void;
}) {
  const timer = useRef(0);
  const held = useRef(false);
  const press = (s: Listed) => ({
    onPointerDown: (e: React.PointerEvent) => {
      if (e.pointerType !== "touch") return;
      held.current = false;
      timer.current = window.setTimeout(() => ((held.current = true), onEdit(s)), 500);
    },
    onPointerUp: () => clearTimeout(timer.current),
    onPointerLeave: () => clearTimeout(timer.current),
    onPointerCancel: () => clearTimeout(timer.current),
    onContextMenu: (e: React.MouseEvent) => e.preventDefault(),
  });
  return (
    <div className="style-grid" role="radiogroup" aria-label="Style">
      <div className="style-tile">
        <button type="button" role="radio" aria-checked={value === ""} onClick={() => onChange("")}>
          <span className="art none">
            <Icon name="ban" size={22} />
          </span>
          <span className="name">None</span>
        </button>
      </div>
      {styles.map((s) => {
        const img = thumbOf(s);
        return (
          <div key={s.id} className="style-tile">
            <button
              type="button"
              role="radio"
              aria-checked={value === s.id}
              onClick={() => (held.current ? (held.current = false) : onChange(s.id))}
              {...press(s)}
            >
              <span className={img ? "art" : "art mine"}>
                {img ? <img src={img} alt="" draggable={false} /> : <Icon name={iconOr(s.icon)} size={22} />}
              </span>
              <span className="name">{s.name}</span>
            </button>
            <button type="button" className="tile-edit" aria-label={`Edit ${s.name}`} onClick={() => onEdit(s)}>
              <Icon name="pencil" size={14} />
            </button>
          </div>
        );
      })}
      <div className="style-tile">
        <button type="button" onClick={onNew}>
          <span className="art add">
            <Icon name="plus" size={22} />
          </span>
          <span className="name">New style</span>
        </button>
      </div>
    </div>
  );
}

/** One prompt (right) and its image forming in place (left). */
function TurnView({
  t,
  onCancel,
  onAgain,
  onEdit,
  onFull,
}: {
  t: Turn;
  onCancel: (id: string) => void;
  onAgain: (t: Turn) => void;
  onEdit: (t: Turn) => void;
  onFull: (t: Turn) => void;
}) {
  const tags = [t.styleName, t.shape].filter(Boolean);
  const frame = { aspectRatio: `${t.w} / ${t.h}`, "--ratio": t.w / t.h, "--p": t.progress } as CSSProperties;
  const label =
    t.status === "queued"
      ? t.queue_position
        ? `Waiting · ${t.queue_position} ahead`
        : "Waiting"
      : t.progress > 0
        ? `Generating · ${Math.round(t.progress * 100)}%`
        : "Starting the engine…";
  return (
    <article className="turn">
      <div className="you">
        <p>{t.body.prompt}</p>
        {tags.length > 0 && (
          <div className="you-tags">
            {tags.map((x) => (
              <span key={x} className="tag">
                {x}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="reply">
        {t.status === "done" && t.image ? (
          <>
            <button type="button" className="shot done" style={frame} onClick={() => onFull(t)} aria-label="Open full screen">
              <img src={`data:image/png;base64,${t.image}`} alt={t.body.prompt} />
            </button>
            <div className="reply-bar">
              <a className="button icon" href={`data:image/png;base64,${t.image}`} download={`glint-${t.seed ?? t.id}-1.png`} aria-label="Download" title="Download">
                <Icon name="download" />
              </a>
              <button type="button" className="icon" aria-label="Try again with a new variation" title="Try again" onClick={() => onAgain(t)}>
                <Icon name="refresh" />
              </button>
              <button type="button" className="icon" aria-label="Edit this prompt" title="Edit prompt" onClick={() => onEdit(t)}>
                <Icon name="pencil" />
              </button>
              <button type="button" className="icon" aria-label="Full screen" title="Full screen" onClick={() => onFull(t)}>
                <Icon name="maximize" />
              </button>
              <span className="muted small num" title="Run time, and time per step (includes loading the model on a first run)">
                {[t.modelName, secs(t.elapsed_ms), `${t.elapsed_ms != null && t.steps ? (t.elapsed_ms / 1000 / t.steps).toFixed(2) : "–"} s/step`, t.seed != null && `variation ${t.seed}`]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </div>
          </>
        ) : finished(t) ? (
          <div className="reply-note">
            <Icon name={t.status === "failed" ? "error" : "x"} size={16} />
            <span>{t.status === "failed" ? t.error?.message ?? "This one failed." : "Cancelled."}</span>
            <button type="button" className="ghost small" onClick={() => onAgain(t)}>
              Try again
            </button>
          </div>
        ) : (
          <div className="shot forming" style={frame} role="status">
            {t.preview && <img className="preview" src={`data:image/jpeg;base64,${t.preview}`} alt="" />}
            <span className="shimmer" aria-hidden="true" />
            <div className="forming-bar">
              <span className="num">{label}</span>
              <button type="button" className="ghost small" onClick={() => onCancel(t.id)}>
                Cancel
              </button>
              <span className="forming-track" aria-hidden="true">
                <span style={{ transform: `scaleX(${t.progress})` }} />
              </span>
            </div>
          </div>
        )}
      </div>
    </article>
  );
}

/** The image on its own, as large as the screen allows. Esc or a click closes it. */
function Lightbox({ t, onClose }: { t: Turn; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);
  return (
    <dialog ref={ref} className="lightbox" aria-label={t.body.prompt} onClose={onClose} onClick={() => ref.current?.close()}>
      <img src={`data:image/png;base64,${t.image}`} alt={t.body.prompt} />
      <button type="button" className="icon" aria-label="Close" autoFocus>
        <Icon name="x" />
      </button>
    </dialog>
  );
}

/** The history as a side sheet, on screens too narrow for it beside the chat. */
/** The chats kept in this browser, newest first: open one, or delete one or all of them. */
function Chats({
  turns,
  current,
  onOpen,
  onDelete,
  onDeleteAll,
}: {
  turns: Turn[];
  current: string;
  onOpen: (id: string) => void;
  onDelete: (id: string, title: string) => void;
  onDeleteAll: () => void;
}) {
  const byChat = new Map<string, Turn[]>();
  for (const t of turns) byChat.set(chatOf(t), [...(byChat.get(chatOf(t)) ?? []), t]);
  const chats = [...byChat].map(([id, ts]) => ({ id, ts, last: ts[ts.length - 1] })).sort((a, b) => b.last.created - a.last.created);
  if (!chats.length) return <p className="muted small">Your chats show up here.</p>;
  return (
    <div className="history">
      <ul className="history-list">
        {chats.map(({ id, ts, last }) => {
          const title = ts[0].body.prompt;
          const image = [...ts].reverse().find((t) => t.image)?.image;
          const n = ts.filter((t) => t.status === "done").length;
          return (
            <li key={id} className="chat-item">
              <button type="button" className="chat-open" aria-current={id === current || undefined} onClick={() => onOpen(id)}>
                <span className="thumb">{image && <img src={`data:image/png;base64,${image}`} alt="" />}</span>
                <span>
                  <span className="chat-title">{title}</span>
                  <span className="meta num">
                    {n === 1 ? "1 image" : `${n} images`} · {new Date(last.created).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}
                  </span>
                </span>
              </button>
              <button type="button" className="icon" aria-label={`Delete chat: ${title}`} title="Delete chat" onClick={() => onDelete(id, title)}>
                <Icon name="trash" size={16} />
              </button>
            </li>
          );
        })}
      </ul>
      <button type="button" className="ghost small" onClick={onDeleteAll}>
        <Icon name="trash" size={16} />
        Delete all chats
      </button>
    </div>
  );
}

function HistorySheet({ onClose, children }: { onClose: () => void; children: React.ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);
  return (
    <dialog ref={ref} className="sheet history-sheet" aria-labelledby="history-title" onClose={onClose}>
      <div className="sheet-head">
        <h2 id="history-title">History</h2>
        <button type="button" className="icon" aria-label="Close" onClick={() => ref.current?.close()}>
          <Icon name="x" />
        </button>
      </div>
      {children}
    </dialog>
  );
}

/** Named sets of the settings above, kept by the host: pick one to apply it, or save, rename and delete. */
function Profiles({ current, onApply }: { current: Omit<Profile, "name">; onApply: (p: Profile) => void }) {
  const [list, setList] = useState<Profile[] | null>(null);
  const [picked, setPicked] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const confirm = useConfirm();
  useEffect(() => {
    api<{ profiles: Profile[] }>("/api/v1/profiles").then((r) => setList(r.profiles), () => setList(null));
  }, []);
  if (!list) return null; // only the main key can keep profiles

  const save = (next: Profile[]) =>
    api<{ profiles: Profile[] }>("/api/v1/profiles", { method: "PUT", body: JSON.stringify({ profiles: next }) }).then(
      (r) => (setList(r.profiles), setError(null), true),
      (e) => (setError(e.message), false),
    );
  const clean = name.trim();
  const same = (p: Profile) => p.name.toLowerCase() === clean.toLowerCase();
  const saveAs = () => {
    const p = { ...current, name: clean };
    save(list.some(same) ? list.map((x) => (same(x) ? p : x)) : [...list, p]).then((ok) => ok && setPicked(p.name));
  };
  const rename = () => save(list.map((x) => (x.name === picked ? { ...x, name: clean } : x))).then((ok) => ok && setPicked(clean));
  const remove = () =>
    confirm.ask({ title: `Delete "${picked}"?`, body: "This removes the saved settings. Images aren't touched.", action: "Delete" }).then(
      (ok) => ok && save(list.filter((x) => x.name !== picked)).then((done) => done && (setPicked(""), setName(""))),
    );

  return (
    <div className="profile-row">
      {confirm.dialog}
      <label>
        Profile
        <select
          value={picked}
          onChange={(e) => {
            const p = list.find((x) => x.name === e.target.value);
            setPicked(e.target.value);
            setName(e.target.value);
            if (p) onApply(p);
          }}
        >
          <option value="">{list.length ? "Pick one to apply" : "None saved yet"}</option>
          {list.map((p) => (
            <option key={p.name}>{p.name}</option>
          ))}
        </select>
      </label>
      <label>
        Name
        <input
          value={name}
          maxLength={60}
          placeholder="e.g. Phone wallpapers"
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && e.preventDefault()} // Enter would make an image
        />
      </label>
      <div className="profile-acts">
        <button type="button" disabled={!clean} onClick={saveAs} title="Save these settings under this name">
          Save
        </button>
        {picked && (
          <>
            <button type="button" disabled={!clean || clean === picked} onClick={rename}>
              Rename
            </button>
            <button type="button" className="danger" onClick={remove}>
              Delete
            </button>
          </>
        )}
      </div>
      {error && <Alert>{error}</Alert>}
    </div>
  );
}
