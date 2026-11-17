import { FormEvent, useEffect, useState } from "react";
import qrcode from "qrcode-generator";
import { api, ApiKey, Connection, Network } from "./api";
import Integrations from "./Integrations";
import { Alert, CopyButton, Icon, Modal, PageHeader, Tabs, useConfirm } from "./ui";

/** Black on white with a quiet zone, whatever the theme: phone cameras read that best. */
function Qr({ text }: { text: string }) {
  const qr = qrcode(0, "M");
  qr.addData(text);
  qr.make();
  const n = qr.getModuleCount();
  let d = "";
  for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (qr.isDark(y, x)) d += `M${x} ${y}h1v1h-1z`;
  return (
    <svg className="qr" viewBox={`-4 -4 ${n + 8} ${n + 8}`} role="img" aria-label="QR code that pairs a phone" shapeRendering="crispEdges">
      <rect x={-4} y={-4} width={n + 8} height={n + 8} fill="#fff" />
      <path d={d} fill="#000" />
    </svg>
  );
}

/** A value to copy: monospace text and an icon button. */
function Copyable({ text, label, shown = text }: { text: string; label: string; shown?: string }) {
  return (
    <div className="copyable">
      <code>{shown}</code>
      <CopyButton text={text} label={label} />
    </div>
  );
}

type Share = { url: string; how: string };

type Draft = { id: string | null; name: string; cloud: boolean; cap: string };
const NEW: Draft = { id: null, name: "", cloud: false, cap: "" };
const when = (t: number | null) => (t ? new Date(t * 1000).toLocaleString() : "Never");

/** Extra keys, one per app or phone: revoke one without touching the others. */
function Keys({ shares }: { shares: Share[] }) {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [pair, setPair] = useState<{ name: string; link: string; how: string } | null>(null);
  const [draft, setDraft] = useState<Draft>(NEW);
  const [made, setMade] = useState<{ name: string; key: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const confirm = useConfirm();

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
  // A phone gets its own local-only key; the QR code opens the phone page with it.
  const pairPhone = (share: Share) => {
    setError(null);
    setMade(null);
    const body = JSON.stringify({ name: `Phone, ${new Date().toLocaleDateString()}`, scopes: ["local"], daily_cap: null });
    api<ApiKey & { key: string }>("/api/v1/keys", { method: "POST", body }).then(
      (k) => (setPair({ name: k.name, link: `${share.url}/m#key=${k.key}`, how: share.how }), load()),
      (e) => setError(e.message),
    );
  };
  const revoke = (k: ApiKey) =>
    confirm
      .ask({ title: `Revoke ${k.name}?`, body: "Apps and phones using this key stop working at once. This can't be undone.", action: "Revoke key" })
      .then((ok) => void (ok &&
    api(`/api/v1/keys/${k.id}`, { method: "DELETE" }).then(() => (draft.id === k.id && setDraft(NEW), load()), (e) => setError(e.message))));

  return (
    <section className="card" aria-labelledby="keys">
      {confirm.dialog}
      <div className="card-head">
        <div>
          <h2 id="keys">Keys for apps and phones</h2>
          <p className="muted small">One key each, so you can stop one without touching the others. These keys only make images.</p>
        </div>
        <div className="actions">
          {shares.map((s) => (
            <button key={s.url} className="small" onClick={() => pairPhone(s)}>
              <Icon name="phone" size={16} />
              Pair a phone {s.how}
            </button>
          ))}
        </div>
      </div>
      {shares.length === 0 && <p className="cap">To pair a phone, turn on one of the sharing options above.</p>}
      {pair && (
        <Modal title="Pair a phone" onClose={() => setPair(null)}>
          <div className="pairing">
            <Qr text={pair.link} />
            <div>
              <p>
                <strong>Scan this with the phone's camera.</strong> It opens Glint Studio on the phone with its own key
                ("{pair.name}"), which can use models on this computer only.
              </p>
              <p className="muted small">
                {pair.how === "anywhere"
                  ? "The public link changes when Glint Studio restarts; pair the phone again then."
                  : "The phone needs to be on the same Wi-Fi as this computer."}{" "}
                Anyone who scans this code can make images, so don't share a picture of it.
              </p>
            </div>
          </div>
          <div className="actions">
            <button value="done" className="primary" autoFocus>
              Done
            </button>
          </div>
        </Modal>
      )}
      {keys.length > 0 && (
        <table className="data">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Models</th>
              <th scope="col" className="n">Daily limit</th>
              <th scope="col" className="n">Last 24 h</th>
              <th scope="col">Last used</th>
              <th scope="col"><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => (
              <tr key={k.id}>
                <td>{k.name}</td>
                <td>{k.scopes.includes("cloud") ? "Local and cloud" : "Local"}</td>
                <td className="n">{k.daily_cap ?? "None"}</td>
                <td className="n">{k.used_24h}</td>
                <td>{when(k.last_used)}</td>
                <td>
                  <div className="actions">
                  <button
                    className="ghost small"
                    onClick={() => (setMade(null), setDraft({ id: k.id, name: k.name, cloud: k.scopes.includes("cloud"), cap: k.daily_cap?.toString() ?? "" }))}
                  >
                    Edit
                  </button>
                  <button className="ghost small danger" onClick={() => revoke(k)}>Revoke</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {made && (
        <div className="key-made" role="status">
          <strong>Copy the key for {made.name} now. It won't be shown again.</strong>
          <Copyable text={made.key} label={`key for ${made.name}`} />
          <div className="actions">
            <button className="small" onClick={() => setMade(null)}>Done</button>
          </div>
        </div>
      )}
      <form className="form key-form" onSubmit={save}>
        <h3>{draft.id ? `Edit "${keys.find((k) => k.id === draft.id)?.name ?? draft.name}"` : "New key"}</h3>
        <div className="two">
          <label>
            Name
            <input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} required maxLength={64} placeholder="My phone" />
          </label>
          <label>
            Images per day
            <input type="number" min={1} max={100000} value={draft.cap} placeholder="No limit" onChange={(e) => setDraft({ ...draft, cap: e.target.value })} />
          </label>
        </div>
        <label className="toggle">
          <input type="checkbox" className="switch" role="switch" checked={draft.cloud} onChange={(e) => setDraft({ ...draft, cloud: e.target.checked })} />
          Cloud models too (they may spend your provider credits)
        </label>
        {error && <Alert>{error}</Alert>}
        <div className="actions">
          <button className="primary" type="submit">{draft.id ? "Save" : "Make key"}</button>
          {draft.id && <button type="button" onClick={() => setDraft(NEW)}>Cancel</button>}
        </div>
      </form>
    </section>
  );
}

/** The same host under each name it answers to. The .local name and the network address only reach it while
 * LAN sharing is on; otherwise it listens on 127.0.0.1 alone. */
function Address({ conn, net }: { conn: Connection; net: Network | null }) {
  const [choice, setChoice] = useState("127.0.0.1");
  const base = new URL(conn.url);
  const at = (host: string) => `${base.protocol}//${host}${base.port ? `:${base.port}` : ""}`;
  const lan = net?.lan.active ?? false;
  const options = [
    { host: "127.0.0.1", usable: true },
    { host: "localhost", usable: true },
    ...(net?.lan.hostname ? [{ host: net.lan.hostname, usable: lan }] : []),
    ...(net?.lan.ip ? [{ host: net.lan.ip, usable: lan }] : []),
  ];
  const picked = options.find((o) => o.host === choice && o.usable) ?? options[0];
  return (
    <>
      <div className="inline-field" style={{ flexWrap: "wrap" }}>
        <select aria-label="Address to show" value={picked.host} onChange={(e) => setChoice(e.target.value)}>
          {options.map((o) => (
            <option key={o.host} value={o.host} disabled={!o.usable}>
              {o.host}
              {o.usable ? "" : " (needs network sharing)"}
            </option>
          ))}
        </select>
        <Copyable text={at(picked.host)} label="address" />
      </div>
      {options.some((o) => !o.usable) && (
        <p className="help">The .local name and network address work once "On your local network" is on, under Share.</p>
      )}
    </>
  );
}

const TUNNEL_STATUS = { off: "", downloading: "Downloading the tunnel tool…", starting: "Opening the link…", running: "", failed: "" };

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
  const dots = "•".repeat(24);

  return (
    <>
      <PageHeader title="Connect" lead="Glint Studio speaks the OpenAI images API, so most apps only need the address and the key" />
      {error && <Alert>{error}</Alert>}

      <section className="card" aria-labelledby="this-computer">
        <h2 id="this-computer">On this computer</h2>
        <dl className="facts wide">
          <dt>Address</dt>
          <dd>
            <Address conn={conn} net={net} />
          </dd>
          <dt>API key</dt>
          <dd>
            <div className="copyable">
              <code>{shown ? conn.api_key : dots}</code>
              <button className="ghost small" onClick={() => setShown(!shown)}>
                {shown ? "Hide" : "Show"}
              </button>
              <CopyButton text={conn.api_key} label="API key" />
            </div>
          </dd>
        </dl>
        <p className="cap">
          Both stay the same when Glint Studio restarts. The key lives in this computer's keychain; to replace it, open Setup
          &amp; Cloud Keys from the Glint Studio menu.
        </p>
      </section>

      {net && (
        <section className="card" aria-labelledby="share">
          <h2 id="share">Share</h2>
          <div>
            <div className="share-row">
              <label className="toggle">
                <input type="checkbox" className="switch" role="switch" checked={net.lan.on} onChange={(e) => share({ lan: e.target.checked })} />
                On your local network
              </label>
              {net.lan.on !== net.lan.active ? (
                <p className="help status quiet" role="status">
                  <Icon name="spinner" size={14} />
                  Restarting the host…
                </p>
              ) : lanUrl ? (
                <Copyable text={lanUrl} label="network address" />
              ) : net.lan.on ? (
                <p className="help">This computer has no network address right now.</p>
              ) : (
                <p className="help">Phones and other computers on your Wi-Fi can use it, with a key.</p>
              )}
            </div>
            <div className="share-row">
              <label className="toggle">
                <input type="checkbox" className="switch" role="switch" checked={net.tunnel.on} onChange={(e) => share({ tunnel: e.target.checked })} />
                Public link
              </label>
              {TUNNEL_STATUS[net.tunnel.status] && (
                <p className="help status quiet" role="status">
                  <Icon name="spinner" size={14} />
                  {TUNNEL_STATUS[net.tunnel.status]}
                </p>
              )}
              {tunnelUrl && <Copyable text={tunnelUrl} label="public link" />}
              {net.tunnel.error && (
                <div className="help">
                  <Alert>{net.tunnel.error}</Alert>
                </div>
              )}
              <p className="help">
                A free Cloudflare link that works from anywhere. Anyone with the link and a key can make images here. It changes
                each time and closes when Glint Studio quits.
              </p>
            </div>
          </div>
        </section>
      )}

      <Keys
        shares={[
          ...(lanUrl ? [{ url: lanUrl, how: "on this Wi-Fi" }] : []),
          ...(tunnelUrl ? [{ url: tunnelUrl, how: "anywhere" }] : []),
        ]}
      />
      <Integrations url={url} />
      <TryIt url={url} apiKey={conn.api_key} shown={shown} dots={dots} />
    </>
  );
}

type Lang = "curl" | "python" | "js";

function TryIt({ url, apiKey, shown, dots }: { url: string; apiKey: string; shown: boolean; dots: string }) {
  const [lang, setLang] = useState<Lang>("curl");
  const code = (key: string): Record<Lang, string> => ({
    curl: `curl ${url}/v1/images/generations \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '{"prompt": "a red fox in the snow", "style": "cartoon"}'`,
    python: `from openai import OpenAI

client = OpenAI(base_url="${url}/v1", api_key="${key}")
image = client.images.generate(
    prompt="a red fox in the snow",
    extra_body={"style": "cartoon"},
)`,
    js: `const res = await fetch("${url}/v1/images/generations", {
  method: "POST",
  headers: { Authorization: "Bearer ${key}", "Content-Type": "application/json" },
  body: JSON.stringify({ prompt: "a red fox in the snow", style: "cartoon" }),
});
const { data } = await res.json();`,
  });
  return (
    <section className="card" aria-labelledby="try">
      <div className="card-head">
        <h2 id="try">Try it</h2>
        <CopyButton text={code(apiKey)[lang]} label="code" />
      </div>
      <Tabs
        variant="underline"
        label="Language"
        value={lang}
        onChange={setLang}
        options={[
          ["curl", "curl"],
          ["python", "Python"],
          ["js", "JavaScript"],
        ]}
      />
      <pre className="snippet">{code(shown ? apiKey : dots)[lang]}</pre>
    </section>
  );
}
