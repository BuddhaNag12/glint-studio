import { useEffect, useState } from "react";
import { api, Connection, Network } from "./api";

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
        <p className="lead note">The address and key stay the same when Glint Studio restarts. The key is kept in this computer's keychain.</p>
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
