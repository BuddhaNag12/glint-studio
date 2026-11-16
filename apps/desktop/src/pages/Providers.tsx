import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import type { HostInfo } from "./Setup";

// Mirrors ProviderStatus in src-tauri/src/secrets.rs. The key itself never comes back from Rust.
type Provider = { id: string; name: string; has_key: boolean };
type Note = { ok: boolean; text: string };

export default function Providers({ host: firstHost }: { host: HostInfo }) {
  const [host, setHost] = useState(firstHost); // saving or removing a key restarts the host
  const [providers, setProviders] = useState<Provider[]>([]);
  const [id, setId] = useState("");
  // The only place the key lives in the UI: cleared once it is saved or the provider changes.
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note | null>(null);

  const refresh = async () => {
    const list = await invoke<Provider[]>("list_providers");
    setProviders(list);
    setId((current) => current || list[0]?.id || "");
  };
  useEffect(() => {
    refresh().catch((e) => setNote({ ok: false, text: String(e) }));
  }, []);

  const selected = providers.find((p) => p.id === id);

  const run = async (action: () => Promise<string>) => {
    setBusy(true);
    setNote(null);
    try {
      setNote({ ok: true, text: await action() });
    } catch (e) {
      setNote({ ok: false, text: String(e) });
    } finally {
      setBusy(false);
    }
  };

  const save = () =>
    run(async () => {
      setHost(await invoke<HostInfo>("set_provider_key", { id, key }));
      setKey("");
      await refresh();
      return "Saved in your system keychain.";
    });

  const remove = () =>
    run(async () => {
      setHost(await invoke<HostInfo>("delete_provider_key", { id }));
      await refresh();
      return "Removed.";
    });

  const test = () =>
    run(async () => {
      const res = await fetch(`${host.url}/api/v1/providers/${encodeURIComponent(id)}/check`, {
        method: "POST",
        headers: { "X-API-Key": host.api_key },
      });
      if (res.ok) return `${selected?.name ?? id} accepted the key.`;
      const body = await res.json().catch(() => null);
      throw new Error(body?.error?.message ?? `The host answered ${res.status}.`);
    });

  return (
    <main className="page">
      <h1>Cloud provider</h1>
      <p className="lead">
        Paste your own API key. It is kept in your system keychain and only sent to the provider.
      </p>

      <section className="card form">
        <label>
          Provider
          <select
            value={id}
            onChange={(e) => {
              setId(e.target.value);
              setKey("");
              setNote(null);
            }}
            disabled={busy}
          >
            {providers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.has_key ? " (key saved)" : ""}
              </option>
            ))}
          </select>
        </label>

        <label>
          API key
          <input
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={selected?.has_key ? "Saved. Paste a new key to replace it." : "Paste your key"}
            autoComplete="off"
            spellCheck={false}
            disabled={busy || !selected}
          />
        </label>

        <div className="actions">
          <button className="primary" onClick={save} disabled={busy || !key.trim()}>
            Save
          </button>
          <button onClick={test} disabled={busy || !selected?.has_key}>
            Test key
          </button>
          <button onClick={remove} disabled={busy || !selected?.has_key}>
            Remove
          </button>
        </div>

        <p className={note?.ok === false ? "error" : "lead"} role="status" aria-live="polite">
          {busy ? "Working…" : note?.text}
        </p>
      </section>

      <section className="card">
        <h2>Dashboard</h2>
        <p className="lead">Activity, models and the address other apps connect to. Glint Studio stays in the menu bar.</p>
        <button
          onClick={() =>
            invoke("open_dashboard").catch((e) => setNote({ ok: false, text: String(e) }))
          }
        >
          Open the dashboard
        </button>
      </section>
    </main>
  );
}
