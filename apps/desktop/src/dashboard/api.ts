// The dashboard is served by the host, so every call is same-origin. The session cookie buys the
// API key once (kept in memory only); everything else uses the key like any other client.

export type Connection = { url: string; api_key: string };

let conn: Connection | null = null;

export async function connect(): Promise<Connection> {
  const res = await fetch("/api/v1/dashboard/connect");
  if (!res.ok) throw new Error("This dashboard link has expired. Open it from the Glint Studio menu again.");
  conn = await res.json();
  return conn!;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = { "Content-Type": "application/json", Authorization: `Bearer ${conn?.api_key}` };
  const res = await fetch(path, { ...init, headers });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  return body as T;
}

/** Job events as they happen; reconnects if the host restarts, calling onOpen each time it's (re)connected so
 * the caller can catch up on what it missed. Returns a function that stops it. */
export function onJobEvents(handle: (e: JobEvent) => void, onOpen?: () => void): () => void {
  let ws: WebSocket | null = null;
  let stopped = false;
  const open = () => {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    // The key rides as a subprotocol, so it never lands in a URL or the host log.
    ws = new WebSocket(`${scheme}://${location.host}/api/v1/events`, ["glint.v1", `glint.key.${conn?.api_key ?? ""}`]);
    ws.onopen = () => onOpen?.();
    ws.onmessage = (m) => handle(JSON.parse(m.data));
    ws.onclose = () => !stopped && setTimeout(open, 3000);
  };
  open();
  return () => {
    stopped = true;
    ws?.close();
  };
}

// Mirror the host: app/jobs.py view(), app/stats.py summary(), app/dashboard.py system().
export type JobEvent = {
  event: string;
  id: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  model: string;
  queue_position: number;
  progress: number;
  created: number;
  elapsed_ms: number | null;
  error: { code: string; message: string } | null;
};

export type Stats = {
  days: { day: number; done: number; failed: number }[];
  totals: { jobs: number; done: number; failed: number; cancelled: number; local: number };
  errors: { code: string; count: number }[];
  models: { model: string; jobs: number; p50_ms: number | null; p95_ms: number | null }[];
  styles: { style: string; count: number }[];
};

export type Hardware = { backend: string; vram_budget_gb: number; ram_gb: number; disk_free_gb: number; gpus?: { name: string }[] };

export type System = {
  engine: { up: boolean; error: string | null; devices: { name: string; type: string; vram_total: number; vram_free: number }[] };
  queue: { running: number; queued: number };
  hardware: Hardware;
  models_bytes: number;
};

// Mirror CatalogModel in app/catalog.py and Style in app/styles.py (the fields the dashboard uses).
export type Model = {
  id: string;
  name: string;
  cloud: boolean;
  defaults: { shape: string; steps: number | null; cfg: number | null };
  quality: Partial<Record<Quality, { steps: number | null; cfg: number | null }>>;
  limits: {
    sizes: Record<string, [number, number]>;
    steps: [number, number] | null;
    cfg: [number, number] | null;
    supports_negative: boolean;
    supports_seed: boolean;
  };
};
export type Quality = "fast" | "standard" | "best";
export type Style = {
  id: string;
  name: string;
  icon: string;
  prefix: string;
  suffix: string;
  negative: string;
  overrides: Record<string, unknown>;
  models: string[] | null;
};

export const gb = (bytes: number) => `${(bytes / 1e9).toFixed(1)} GB`;
export const secs = (ms: number | null) => (ms == null ? "–" : `${(ms / 1000).toFixed(1)} s`);
