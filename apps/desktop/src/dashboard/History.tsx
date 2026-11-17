import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { Alert, Icon } from "./ui";

// Mirrors History.list in host/app/history.py. created is unix seconds; width to negative come from the job.
export type HistoryItem = {
  id: string;
  created: number;
  prompt: string;
  model: string;
  style: string | null;
  seed: number | null;
  elapsed_ms: number | null;
  width?: number;
  height?: number;
  steps?: number | null;
  cfg?: number | null;
  negative?: string | null;
};

// shortcut: thumbnails are the full images (the host has no image library to shrink them), fetched as they
// scroll into view and the last 60 kept; fine on this computer, slow over the public link.
const seen = new Map<string, Promise<string>>();
export function imageOf(id: string): Promise<string> {
  let p = seen.get(id);
  if (!p) {
    p = api<{ b64_json: string }>(`/api/v1/history/${id}`).then((r) => r.b64_json);
    p.catch(() => seen.delete(id));
    seen.set(id, p);
    if (seen.size > 60) seen.delete(seen.keys().next().value!);
  }
  return p;
}

const when = (s: number) => new Date(s * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });

/** Past images from every session, newest first, with search. `refresh` changes when a new image is made. */
export default function History({
  refresh,
  names,
  onOpen,
  onUse,
  onAgain,
}: {
  refresh: number;
  names: (h: HistoryItem) => { model: string; style: string };
  onOpen: (h: HistoryItem, image: string) => void;
  onUse: (h: HistoryItem) => void;
  onAgain: (h: HistoryItem) => void;
}) {
  const [q, setQ] = useState("");
  const [items, setItems] = useState<HistoryItem[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const load = (before?: string) => {
    const n = ++seq.current; // a slower, older search never overwrites a newer one
    const query = `q=${encodeURIComponent(q.trim())}&limit=30${before ? `&before=${before}` : ""}`;
    api<{ items: HistoryItem[]; next: string | null }>(`/api/v1/history?${query}`).then(
      (r) => {
        if (n !== seq.current) return;
        setItems((all) => (before ? [...(all ?? []), ...r.items] : r.items));
        setNext(r.next);
        setError(null);
      },
      (e) => n === seq.current && setError(e.message),
    );
  };
  useEffect(() => {
    const timer = setTimeout(load, q ? 250 : 0);
    return () => clearTimeout(timer);
  }, [q, refresh]);

  return (
    <div className="history">
      <input type="search" aria-label="Search past images" placeholder="Search prompts" value={q} onChange={(e) => setQ(e.target.value)} />
      {error && <Alert>{error}</Alert>}
      {items?.length === 0 && <p className="muted small">{q.trim() ? "No images match." : "Images you make show up here."}</p>}
      <ul className="history-list">
        {items?.map((h) => {
          const { model, style } = names(h);
          return (
            <li key={h.id} className="history-item">
              <button type="button" className="thumb" aria-label={`Open: ${h.prompt}`} onClick={() => imageOf(h.id).then((b) => onOpen(h, b), (e) => setError(e.message))}>
                <Thumb id={h.id} />
              </button>
              <div>
                <p title={h.prompt}>{h.prompt}</p>
                <span className="meta num">{[model, style, h.seed != null && `variation ${h.seed}`, when(h.created)].filter(Boolean).join(" · ")}</span>
                <div className="acts">
                  <button type="button" className="icon" aria-label="Use these settings" title="Use these settings" onClick={() => onUse(h)}>
                    <Icon name="pencil" size={16} />
                  </button>
                  <button type="button" className="icon" aria-label="Make a new variation" title="Try again" onClick={() => onAgain(h)}>
                    <Icon name="refresh" size={16} />
                  </button>
                </div>
              </div>
            </li>
          );
        })}
      </ul>
      {next && (
        <button type="button" className="ghost small" onClick={() => load(next)}>
          Show more
        </button>
      )}
    </div>
  );
}

/** The image, fetched once it's near the screen. */
function Thumb({ id }: { id: string }) {
  const [src, setSrc] = useState<string>();
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let live = true;
    const io = new IntersectionObserver(
      ([e]) => {
        if (!e.isIntersecting) return;
        io.disconnect();
        imageOf(id).then((b) => live && setSrc(b), () => {});
      },
      { rootMargin: "200px" },
    );
    io.observe(el);
    return () => {
      live = false;
      io.disconnect();
    };
  }, [id]);
  return <span ref={ref}>{src && <img src={`data:image/png;base64,${src}`} alt="" />}</span>;
}
