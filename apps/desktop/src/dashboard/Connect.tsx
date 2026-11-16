import { FormEvent, useEffect, useState } from "react";
import { api, ApiKey, Connection, Network } from "./api";

function Copy({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      aria-label={`Copy ${label}`}
      onClick={() =>
        navigator.clipboard.writeText(text).then(() => {
          setDone(true);
          setTimeout(() => setDone(false), 1500);
        })
      }
    >
      {done ? "Copied" : "Copy"}
    </button>
  );
}

type Draft = { id: string | null; name: string; cloud: boolean; cap: string };
const NEW: Draft = { id: null, name: "", cloud: false, cap: "" };
const when = (t: number | null) => (t ? new Date(t * 1000).toLocaleString() : "Never");

/** Extra keys, one per app or phone: revoke one without touching the others. */
function Keys() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [draft, setDraft] = useState<Draft>(NEW);
  const [made, setMade] = useState<{ name: string; key: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => api<{ keys: ApiKey[] }>("/api/v1/keys").then((r) => setKeys(r.keys), (e) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

  const save = (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    const body = JSON.stringify({
      name: draft.name.trim(),
      scopes: draft.cloud ? ["local", "cloud"] : ["local"],
      daily_cap: draft.cap ? Number(draft.cap) : null,
    });
    const done = draft.id
      ? api(`/api/v1/keys/${draft.id}`, { method: "PUT", body })
      : api<ApiKey & { key: string }>("/api/v1/keys", { method: "POST", body }).then((k) => setMade({ name: k.name, key: k.key }));
    done.then(() => (setDraft(NEW), load()), (e) => setError(e.message));
  };
  const revoke = (k: ApiKey) =>
    confirm(`Revoke "${k.name}"? Apps using it stop working at once.`) &&
    api(`/api/v1/keys/${k.id}`, { method: "DELETE" }).then(() => (draft.id === k.id && setDraft(NEW), load()), (e) => setError(e.message));

  return (
    <section className="card">
      <h2>Keys for apps and phones</h2>
      <p className="lead">
        Give each app or phone its own key, so you can stop one without changing the others. These keys can only make
        images; settings need the main key above.
      </p>
      {keys.length > 0 && (
        <table className="data keys">
          <thead>
            <tr>
              <th>Name</th>
              <th>Models</th>
              <th>Daily limit</th>
              <th>Last 24 h</th>
              <th>Last used</th>
              <th><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.id}>
                <td>{k.name}</td>
                <td>{k.scopes.includes("cloud") ? "Local and cloud" : "Local"}</td>
                <td>{k.daily_cap ?? "None"}</td>
                <td>{k.used_24h}</td>
                <td>{when(k.last_used)}</td>
                <td>
                  <div className="actions">
                  <button
                    onClick={() => (setMade(null), setDraft({ id: k.id, name: k.name, cloud: k.scopes.includes("cloud"), cap: k.daily_cap?.toString() ?? "" }))}
                  >
                    Edit
                  </button>
                  <button onClick={() => revoke(k)}>Revoke</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {made && (
        <div className="made" role="status">
          <p>
            <strong>Copy the key for {made.name} now.</strong> It won't be shown again.
          </p>
          <p className="row">
            <code>{made.key}</code>
            <Copy text={made.key} label={`key for ${made.name}`} />
            <button onClick={() => setMade(null)}>Done</button>
          </p>
        </div>
      )}
      <form className="form key-form" onSubmit={save}>
        <h3>{draft.id ? `Edit "${keys.find((k) => k.id === draft.id)?.name ?? draft.name}"` : "New key"}</h3>
        <div className="split">
          <label>
            Name
            <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} required maxLength={64} placeholder="My phone" />
          </label>
          <label>
            Images per day (empty for no limit)
            <input type="number" min={1} max={100000} value={draft.cap} onChange={(e) => setDraft({ ...draft, cap: e.target.value })} />
          </label>
        </div>
        <label className="switch">
          <input type="checkbox" checked={draft.cloud} onChange={(e) => setDraft({ ...draft, cloud: e.target.checked })} />
          Cloud models too (they may spend your provider credits)
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="actions">
          <button className="primary" type="submit">{draft.id ? "Save" : "Make key"}</button>
          {draft.id && <button type="button" onClick={() => setDraft(NEW)}>Cancel</button>}
        </div>
      </form>
    </section>
  );
}

const TUNNEL_STATUS ={ off: "", downloading: "Downloading the tunnel tool…", starting: "Opening the link…", running: "", failed: "" };

export default function Connect({ conn }: { conn: Connection }) {
  const [shown, setShown] = useState(false);
  const [net, setNet] = useState<Network | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The host restarts when the LAN setting changes, so a failed poll just waits for the next one.
  useEffect(() => {
    const load = () => api<Network>("/api/v1/network").then(setNet, () => {});
    load();
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, []);
  const share = (change: Partial<Record<"lan" | "tunnel", boolean>>) => {
    setError(null);
    api<Network>("/api/v1/network", { method: "PUT", body: JSON.stringify(change) }).then(setNet, (e) => setError(e.message));
  };

  const lanUrl = net?.lan.active ? net.lan.url : null;
  const tunnelUrl = net?.tunnel.status === "running" ? net.tunnel.url : null;
  const url = tunnelUrl ?? lanUrl ?? conn.url;
  const curl = `curl ${url}/v1/images/generations \\
  -H "Authorization: Bearer ${conn.api_key}" \\
  -H "Content-Type: application/json" \\
  -d '{"prompt": "a red fox in the snow", "style": "cartoon"}'`;

  return (
    <>
      <h1>Connect</h1>
      <p className="lead">
        Other apps on this computer reach Glint Studio here. It speaks the OpenAI images API, so most clients only need the
        address and the key.
      </p>
      <section className="card">
        <dl className="facts">
          <dt>Address</dt>
          <dd className="row">
            <code>{conn.url}</code>
            <Copy text={conn.url} label="address" />
          </dd>
          <dt>API key</dt>
          <dd className="row">
            <code>{shown ? conn.api_key : "•".repeat(24)}</code>
            <button onClick={() => setShown(!shown)}>{shown ? "Hide" : "Show"}</button>
            <Copy text={conn.api_key} label="API key" />
          </dd>
        </dl>
        <p className="lead note">
          The address and key stay the same when Glint Studio restarts. The key is kept in this computer's keychain; to replace
          it, use Setup &amp; Cloud Keys in the Glint Studio menu.
        </p>
      </section>
      {net && (
        <section className="card share">
          <h2>Share</h2>
          <div>
            <label className="switch">
              <input type="checkbox" checked={net.lan.on} onChange={(e) => share({ lan: e.target.checked })} />
              On your local network
            </label>
            {net.lan.on !== net.lan.active ? (
              <p className="muted" role="status">Restarting the host…</p>
            ) : lanUrl ? (
              <p className="row">
                <code>{lanUrl}</code>
                <Copy text={lanUrl} label="network address" />
              </p>
            ) : net.lan.on ? (
              <p className="muted">This computer has no network address right now.</p>
            ) : (
              <p className="muted">Phones and other computers on your Wi-Fi can use it, with the key.</p>
            )}
          </div>
          <div>
            <label className="switch">
              <input type="checkbox" checked={net.tunnel.on} onChange={(e) => share({ tunnel: e.target.checked })} />
              Public link
            </label>
            {TUNNEL_STATUS[net.tunnel.status] && <p className="muted" role="status">{TUNNEL_STATUS[net.tunnel.status]}</p>}
            {tunnelUrl && (
              <p className="row">
                <code>{tunnelUrl}</code>
                <Copy text={tunnelUrl} label="public link" />
              </p>
            )}
            {net.tunnel.error && <pre className="error snippet">{net.tunnel.error}</pre>}
            <p className="muted">
              A free Cloudflare link that works from anywhere. Anyone with the link and the key can make images on this computer.
              The link changes each time and closes when Glint Studio quits.
            </p>
          </div>
          {error && <p className="error" role="alert">{error}</p>}
        </section>
      )}
      <Keys />
      <section className="card">
        <div className="model-head">
          <h2>Try it</h2>
          <Copy text={curl} label="curl command" />
        </div>
        <pre className="snippet">{shown ? curl : curl.replace(conn.api_key, "•".repeat(24))}</pre>
      </section>
    </>
  );
}
