import * as P from "@phosphor-icons/react";
import { ReactNode, useEffect, useId, useRef, useState } from "react";

// Phosphor icons (MIT), drawn duotone: a line icon over a soft fill of the same colour, friendly but still crisp.
const ICONS = {
  activity: P.Pulse,
  image: P.Image,
  layers: P.Stack,
  box: P.Cube,
  plug: P.Plug,
  check: P.Check,
  warn: P.Warning,
  error: P.WarningCircle,
  spinner: P.CircleNotch,
  up: P.ArrowUp,
  down: P.ArrowDown,
  chevron: P.CaretDown,
  x: P.X,
  copy: P.Copy,
  download: P.DownloadSimple,
  cpu: P.Cpu,
  monitor: P.Monitor,
  globe: P.Globe,
  key: P.Key,
  terminal: P.TerminalWindow,
  phone: P.DeviceMobile,
  smartphone: P.DeviceMobile,
  folder: P.Folder,
  plus: P.Plus,
  pencil: P.PencilSimple,
  refresh: P.ArrowsClockwise,
  maximize: P.ArrowsOut,
  sliders: P.SlidersHorizontal,
  newchat: P.NotePencil,
  trash: P.Trash,
  ban: P.Prohibit,
  square: P.Stop,
  sparkles: P.Sparkle,
  smile: P.Smiley,
  star: P.Star,
  film: P.FilmStrip,
  camera: P.Camera,
  grid: P.GridFour,
  droplet: P.Drop,
  settings: P.GearSix,
  gauge: P.Gauge,
  palette: P.Palette,
};
export type IconName = keyof typeof ICONS;
/** A style's icon field is free text; anything not drawn here falls back to a palette. */
export const iconOr = (name: string): IconName => (name in ICONS ? (name as IconName) : "palette");
// Glyphs with no inner area read better as a bold line than duotone.
const BOLD = new Set<IconName>(["check", "spinner", "up", "down", "chevron", "x", "plus"]);

/** Decorative unless it has a label. */
export function Icon({ name, size = 18, label, className }: { name: IconName; size?: number; label?: string; className?: string }) {
  const Glyph = ICONS[name];
  return (
    <Glyph
      className={`icon-svg${name === "spinner" ? " spin" : ""}${className ? ` ${className}` : ""}`}
      size={size}
      weight={BOLD.has(name) ? "bold" : "duotone"}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    />
  );
}

export function PageHeader({ title, lead, children }: { title: string; lead?: ReactNode; children?: ReactNode }) {
  return (
    <header className="page-head">
      <div>
        <h1>{title}</h1>
        {lead && <p className="lead">{lead}</p>}
      </div>
      {children}
    </header>
  );
}

export function Alert({ children, info }: { children: ReactNode; info?: boolean }) {
  return (
    <div className={info ? "alert info" : "alert"} role={info ? "status" : "alert"}>
      <Icon name="error" />
      <div>{children}</div>
    </div>
  );
}

/** value is 0–1. */
export function Progress({ value, label }: { value: number; label: string }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className="track" role="progressbar" aria-label={label} aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
      <span style={{ width: `${pct}%` }} />
    </div>
  );
}

export type Fit = "great" | "slow" | "wont_fit";
export const FIT: Record<Fit, string> = { great: "Runs great", slow: "Runs slow", wont_fit: "Won't fit" };

/** Memory a model needs against what this computer has for models. The tick is the budget, at 80% of the
 * track, so a model that doesn't fit visibly runs past it. */
export function FitGauge({ fit, need, budget, compact }: { fit: Fit; need: number; budget: number; compact?: boolean }) {
  const scale = budget * 1.25;
  return (
    <span
      className={`gauge ${fit}${compact ? " compact" : ""}`}
      role="meter"
      aria-label="GPU memory needed"
      aria-valuenow={need}
      aria-valuemin={0}
      aria-valuemax={scale}
      aria-valuetext={`${need.toFixed(1)} of ${budget.toFixed(1)} GB`}
    >
      <span className="need" style={{ width: `${Math.min(100, (need / scale) * 100)}%` }} />
      <span className="tick" />
    </span>
  );
}

export function FitLabel({ fit, children }: { fit: Fit; children?: ReactNode }) {
  return (
    <span className={`status num${fit === "slow" ? " warn" : fit === "wont_fit" ? " bad" : ""}`}>
      <Icon name={fit === "great" ? "check" : fit === "slow" ? "warn" : "error"} size={14} />
      {FIT[fit]}
      {children}
    </span>
  );
}

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="icon"
      aria-label={done ? "Copied" : `Copy ${label}`}
      title={done ? "Copied" : `Copy ${label}`}
      onClick={() =>
        navigator.clipboard.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        })
      }
    >
      <Icon name={done ? "check" : "copy"} />
    </button>
  );
}

/** A tab set. "segmented" switches views; "underline" switches content inside a card. */
export function Tabs<T extends string>({
  value,
  options,
  onChange,
  label,
  variant = "segmented",
}: {
  value: T;
  options: [T, ReactNode][];
  onChange: (v: T) => void;
  label: string;
  variant?: "segmented" | "underline";
}) {
  return (
    <div role="tablist" aria-label={label} className={variant === "segmented" ? "segmented" : "tabs"}>
      {options.map(([id, text]) => (
        <button key={id} type="button" role="tab" aria-selected={id === value} onClick={() => onChange(id)}>
          {text}
        </button>
      ))}
    </div>
  );
}

/** A native modal dialog: focus trap, Esc and focus return come with it. Mount it to open it. onClose gets
 * the value of the button that closed it ("" for Esc). */
export function Modal({ title, onClose, children }: { title: string; onClose: (value: string) => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  }, []);
  return (
    <dialog ref={ref} className="modal" aria-labelledby={id} onClose={(e) => onClose(e.currentTarget.returnValue)}>
      {/* React bubbles submit up its own tree: a modal rendered inside another form must not submit it. */}
      <form method="dialog" onSubmit={(e) => e.stopPropagation()}>
        <h2 id={id}>{title}</h2>
        {children}
      </form>
    </dialog>
  );
}

type Ask = { title: string; body: string; action: string };

/** Replaces window.confirm, which the app's webview may not show. Render `dialog`, await `ask(...)`. */
export function useConfirm() {
  const [q, setQ] = useState<(Ask & { resolve: (ok: boolean) => void }) | null>(null);
  const ask = (a: Ask) => new Promise<boolean>((resolve) => setQ({ ...a, resolve }));
  const dialog = q && (
    <Modal
      title={q.title}
      onClose={(v) => {
        q.resolve(v === "ok");
        setQ(null);
      }}
    >
      <p>{q.body}</p>
      <div className="actions">
        <button value="cancel" autoFocus>
          Cancel
        </button>
        <button value="ok" className="danger">
          {q.action}
        </button>
      </div>
    </Modal>
  );
  return { dialog, ask };
}

/** A Settings page card. badge: "live" applies at once, "restart" needs an engine restart. */
export function SettingsSection({
  id,
  title,
  badge,
  onReset,
  children,
}: {
  id: string;
  title: string;
  badge?: "live" | "restart";
  onReset?: () => void;
  children: ReactNode;
}) {
  return (
    <section id={id} className="card set-section" aria-labelledby={`${id}-title`}>
      <div className="set-head">
        <h2 id={`${id}-title`}>{title}</h2>
        {badge && <span className={`set-badge ${badge}`}>{badge === "live" ? "Live" : "Restart needed"}</span>}
        {onReset && (
          <button type="button" className="ghost small set-reset" onClick={onReset}>
            Reset to default
          </button>
        )}
      </div>
      {children}
    </section>
  );
}

/** One setting: label and hint on the left, the control on the right. Pass `id` when the control is a single
 * input with that id, so the label clicks through to it; otherwise the control is grouped under the label. */
export function SettingRow({ label, hint, id, children }: { label: string; hint?: ReactNode; id?: string; children: ReactNode }) {
  const own = useId();
  const labelId = `${own}-label`;
  const hintId = hint ? `${own}-hint` : undefined;
  return (
    <div className="set-row">
      <div className="set-text">
        {id ? (
          <label id={labelId} htmlFor={id}>
            {label}
          </label>
        ) : (
          <span id={labelId}>{label}</span>
        )}
        {hint && (
          <p id={hintId} className="help">
            {hint}
          </p>
        )}
      </div>
      <div className="set-control" role={id ? undefined : "group"} aria-labelledby={id ? undefined : labelId} aria-describedby={hintId}>
        {children}
      </div>
    </div>
  );
}
