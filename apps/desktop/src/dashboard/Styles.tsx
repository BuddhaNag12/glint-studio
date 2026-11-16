import { FormEvent, useEffect, useRef, useState } from "react";
import { api, Style } from "./api";

type Listed = Style & { builtin: boolean; edited: boolean };

const SAMPLE = "a red fox in the snow";

/** Mirrors compose() in host/app/styles.py: prefix. prompt. suffix, without doubled periods. */
function preview(s: Pick<Style, "prefix" | "suffix">, prompt: string) {
  const parts = [s.prefix, prompt, s.suffix].map((p) => p.trim()).filter(Boolean);
  return [...parts.slice(0, -1).map((p) => p.replace(/\.$/, "").trimEnd()), ...parts.slice(-1)].join(". ");
}

const BLANK: Style = { id: "", name: "", icon: "", prefix: "", suffix: "", negative: "", overrides: {}, models: null };

const slug = (name: string) => name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "style";

export default function Styles() {
  const [styles, setStyles] = useState<Listed[]>([]);
  const [draft, setDraft] = useState<Style | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [sample, setSample] = useState(SAMPLE);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);

  const load = () =>
    api<{ styles: Listed[] }>("/api/v1/styles").then((r) => {
      setStyles(r.styles);
      return r.styles;
    }, (e) => (setError(e.message), []));
  useEffect(() => {
    load();
  }, []);

  const current = styles.find((s) => s.id === draft?.id);
  const pick = (s: Style, fresh = false) => {
    const { builtin: _b, edited: _e, ...style } = s as Listed;
    setDraft(style);
    setIsNew(fresh);
    // On a phone the editor sits under the list.
    if (matchMedia("(max-width: 640px)").matches)
      requestAnimationFrame(() => document.querySelector(".styles form")?.scrollIntoView({ block: "start" }));
    setError(null);
    setSaved(null);
  };
  const set = (field: keyof Style) => (e: { target: { value: string } }) => draft && setDraft({ ...draft, [field]: e.target.value });

  const save = (e: FormEvent) => {
    e.preventDefault();
    if (!draft) return;
    if (isNew && styles.some((s) => s.id === draft.id)) return setError(`There's already a style with the id "${draft.id}".`);
    api(`/api/v1/styles/${draft.id}`, { method: "PUT", body: JSON.stringify(draft) }).then(
      () => (setIsNew(false), setSaved("Saved."), load()),
      (e) => setError(e.message),
    );
  };
  const remove = () =>
    draft &&
    api(`/api/v1/styles/${draft.id}`, { method: "DELETE" }).then(
      () =>
        load().then((all) => {
          const back = all.find((s) => s.id === draft.id); // a built-in comes back as shipped
          back ? (pick(back), setSaved("Back to the default.")) : setDraft(null);
        }),
      (e) => setError(e.message),
    );
  const copy = (s: Style, name: string) => {
    let id = slug(name);
    for (let i = 2; styles.some((x) => x.id === id); i++) id = `${slug(name)}-${i}`;
    pick({ ...s, id, name }, true);
  };

  // An imported file opens as a new, unsaved style; the host checks every field when it's saved.
  const importFile = (file: File | undefined) =>
    file?.text().then(
      (text) => {
        const style = { ...BLANK, ...JSON.parse(text) } as Style;
        if (!styles.some((s) => s.id === style.id)) return pick(style, true);
        copy(style, style.name || "Imported style"); // keep the existing one; give this a free id
      },
    ).catch(() => setError("That file isn't a style (it should be the JSON a style exports)."));

  return (
    <>
      <h1>Styles</h1>
      <p className="lead">A style wraps the prompt with its own words and a negative. Apps pick one with the style parameter.</p>
      <div className="split styles">
        <section className="card">
          <div className="model-head">
            <h2>All styles</h2>
            <div className="actions">
              <button onClick={() => picker.current?.click()}>Import</button>
              <input ref={picker} type="file" accept="application/json,.json" hidden onChange={(e) => (importFile(e.target.files?.[0]), (e.target.value = ""))} />
              <button onClick={() => copy(BLANK, "New style")}>New</button>
            </div>
          </div>
          <ul className="style-list">
            {styles.map((s) => (
              <li key={s.id}>
                <button aria-current={s.id === draft?.id && !isNew ? "true" : undefined} onClick={() => pick(s)}>
                  {s.name}
                  <span className="muted">{s.builtin ? (s.edited ? " · edited" : "") : " · yours"}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>

        {draft ? (
          <form className="card form" onSubmit={save}>
            <h2>{isNew ? "New style" : draft.name}</h2>
            <div className="split">
              <label>
                Name
                <input value={draft.name} onChange={set("name")} required maxLength={64} />
              </label>
              <label>
                Icon name
                <input value={draft.icon} onChange={set("icon")} maxLength={32} />
              </label>
            </div>
            {isNew && (
              <label>
                Id (what apps send as style)
                <input value={draft.id} onChange={set("id")} required pattern="[a-z0-9][a-z0-9_\-]*" />
              </label>
            )}
            <label>
              Before the prompt
              <textarea value={draft.prefix} onChange={set("prefix")} rows={2} maxLength={4000} />
            </label>
            <label>
              After the prompt
              <textarea value={draft.suffix} onChange={set("suffix")} rows={2} maxLength={4000} />
            </label>
            <label>
              Negative (added to the model's and the app's)
              <textarea value={draft.negative} onChange={set("negative")} rows={2} maxLength={4000} />
            </label>
            <label>
              Try it with
              <input value={sample} onChange={(e) => setSample(e.target.value)} />
            </label>
            <pre className="snippet" aria-label="The prompt the model gets">{preview(draft, sample)}</pre>
            {error && <p className="error" role="alert">{error}</p>}
            {saved && <p role="status">{saved}</p>}
            <div className="actions">
              <button className="primary" type="submit">Save</button>
              {!isNew && <button type="button" onClick={() => copy(draft, `${draft.name} copy`)}>Duplicate</button>}
              <a className="button" href={`data:application/json,${encodeURIComponent(JSON.stringify(draft, null, 2))}`} download={`${draft.id || "style"}.json`}>
                Export
              </a>
              {current?.builtin && current.edited && <button type="button" onClick={remove}>Reset to default</button>}
              {current && !current.builtin && (
                <button type="button" onClick={() => confirm(`Delete "${current.name}"? This can't be undone.`) && remove()}>
                  Delete
                </button>
              )}
            </div>
          </form>
        ) : (
          <section className="card">
            <p className="lead">Pick a style to edit it, or make a new one.</p>
            {error && <p className="error">{error}</p>}
          </section>
        )}
      </div>
    </>
  );
}
