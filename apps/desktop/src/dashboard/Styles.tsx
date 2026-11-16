import { FormEvent, useEffect, useRef, useState } from "react";
import { api, Style } from "./api";
import { Alert, Icon, useConfirm } from "./ui";

export type Listed = Style & { builtin: boolean; edited: boolean };

const SAMPLE = "a red fox in the snow";

/** Mirrors compose() in host/app/styles.py: prefix. prompt. suffix, without doubled periods. */
function preview(s: Pick<Style, "prefix" | "suffix">, prompt: string) {
  const parts = [s.prefix, prompt, s.suffix].map((p) => p.trim()).filter(Boolean);
  return [...parts.slice(0, -1).map((p) => p.replace(/\.$/, "").trimEnd()), ...parts.slice(-1)].join(". ");
}

export const BLANK: Style = { id: "", name: "", icon: "", prefix: "", suffix: "", negative: "", overrides: {}, models: null };

const slug = (name: string) => name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "style";

/** A copy of `s` named `name`, with an id no other style has. */
export function copyOf(s: Style, name: string, styles: Style[]): Style {
  let id = slug(name);
  for (let i = 2; styles.some((x) => x.id === id); i++) id = `${slug(name)}-${i}`;
  return { ...s, id, name };
}

const strip = ({ builtin: _b, edited: _e, ...style }: Style & Partial<Listed>): Style => style;

/** Edits one style in a side panel (a bottom sheet on a phone). `reload` refetches the list; `onSaved` gets
 * the id of a style that was just saved. A built-in style is edited as a copy and can be reset. */
export default function StyleEditor({
  start,
  fresh,
  styles,
  reload,
  onSaved,
  onClose,
}: {
  start: Style;
  fresh: boolean;
  styles: Listed[];
  reload: () => Promise<Listed[]>;
  onSaved: (id: string) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState<Style>(strip(start));
  const [isNew, setIsNew] = useState(fresh);
  const [sample, setSample] = useState(SAMPLE);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const ref = useRef<HTMLDialogElement>(null);
  const confirm = useConfirm();

  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);

  const current = styles.find((s) => s.id === draft.id);
  const open = (s: Style, asNew: boolean, note: string | null = null) => {
    setDraft(strip(s));
    setIsNew(asNew);
    setError(null);
    setSaved(note);
  };
  const set = (field: keyof Style) => (e: { target: { value: string } }) => setDraft({ ...draft, [field]: e.target.value });

  const save = (e: FormEvent) => {
    e.preventDefault();
    if (isNew && styles.some((s) => s.id === draft.id)) return setError(`There's already a style with the id "${draft.id}".`);
    api(`/api/v1/styles/${draft.id}`, { method: "PUT", body: JSON.stringify(draft) }).then(
      () => (setIsNew(false), setSaved("Saved."), reload(), onSaved(draft.id)),
      (e) => setError(e.message),
    );
  };
  const remove = () =>
    api(`/api/v1/styles/${draft.id}`, { method: "DELETE" }).then(
      () =>
        reload().then((all) => {
          const back = all.find((s) => s.id === draft.id); // a built-in comes back as shipped
          back ? open(back, false, "Back to the default.") : ref.current?.close();
        }),
      (e) => setError(e.message),
    );

  // An imported file opens as a new, unsaved style; the host checks every field when it's saved.
  const importFile = (file: File | undefined) =>
    file
      ?.text()
      .then((text) => {
        const style = { ...BLANK, ...JSON.parse(text) } as Style;
        open(styles.some((s) => s.id === style.id) ? copyOf(style, style.name || "Imported style", styles) : style, true);
      })
      .catch(() => setError("That file isn't a style (it should be the JSON a style exports)."));

  return (
    <dialog ref={ref} className="sheet" aria-labelledby="style-title" onClose={onClose}>
      {confirm.dialog}
      <form className="form" onSubmit={save}>
        <div className="sheet-head">
          <h2 id="style-title">{isNew ? "New style" : draft.name || "Style"}</h2>
          {current && (current.edited || !current.builtin) && <span className="tag">{current.builtin ? "Edited" : "Yours"}</span>}
          <button type="button" className="icon" aria-label="Close" onClick={() => ref.current?.close()}>
            <Icon name="x" />
          </button>
        </div>
        <p className="muted small">A style wraps the prompt in its own words and adds a negative. Apps pick one with the style parameter.</p>
        <div className="two">
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
          Negative
          <textarea value={draft.negative} onChange={set("negative")} rows={2} maxLength={4000} />
          <span className="help">Added to the model's and the app's, never replacing them</span>
        </label>
        <label>
          Try it with
          <input value={sample} onChange={(e) => setSample(e.target.value)} />
        </label>
        <div className="field">
          The prompt the model gets
          <pre className="snippet">{preview(draft, sample)}</pre>
        </div>
        {error && <Alert>{error}</Alert>}
        {saved && (
          <p className="status" role="status">
            <Icon name="check" size={14} />
            {saved}
          </p>
        )}
        <div className="actions">
          <button className="primary" type="submit">
            Save
          </button>
          {!isNew && (
            <button type="button" onClick={() => open(copyOf(draft, `${draft.name} copy`, styles), true)}>
              Duplicate
            </button>
          )}
          <a className="button" href={`data:application/json,${encodeURIComponent(JSON.stringify(draft, null, 2))}`} download={`${draft.id || "style"}.json`}>
            Export
          </a>
          {isNew && (
            <button type="button" onClick={() => picker.current?.click()}>
              Import a file
            </button>
          )}
          <input ref={picker} type="file" accept="application/json,.json" hidden onChange={(e) => (importFile(e.target.files?.[0]), (e.target.value = ""))} />
          {current?.builtin && current.edited && (
            <button type="button" onClick={remove}>
              Reset to default
            </button>
          )}
          {current && !current.builtin && (
            <button
              type="button"
              className="danger"
              onClick={() =>
                confirm
                  .ask({ title: `Delete ${current.name}?`, body: "Apps that send this style will get an error. This can't be undone.", action: "Delete style" })
                  .then((ok) => void (ok && remove()))
              }
            >
              Delete
            </button>
          )}
        </div>
      </form>
    </dialog>
  );
}
