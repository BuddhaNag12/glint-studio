import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import type { HostInfo } from "./Setup";
import { Alert, Icon, useConfirm } from "../dashboard/ui";

// Mirrors ProviderStatus in src-tauri/src/secrets.rs. The key itself never comes back from Rust.
type Provider = { id: string; name: string; has_key: boolean };
type Note = { ok: boolean; text: string };

/** Keep `busy`, the note and a try/catch around one action at a time. */
function useRun() {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note | null>(null);
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
  const view = (
    <div aria-live="polite">
      {busy ? (
        <p className="status quiet">
          <Icon name="spinner" size={14} />
          Working…
        </p>
      ) : note?.ok === false ? (
        <Alert>{note.text}</Alert>
      ) : note ? (
        <p className="status" role="status">
          <Icon name="check" size={14} />
          {note.text}
        </p>
      ) : null}
    </div>
  );
  return { busy, run, setNote, view };
}

/** A cloud provider's API key: saving or removing one restarts the host, which `onHost` hears about. */
export function ProviderKeys({ host: firstHost, onHost }: { host: HostInfo; onHost?: (h: HostInfo) => void }) {
  const [host, setHost] = useState(firstHost);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [id, setId] = useState("");
  // The only place the key lives in the UI: cleared once it is saved or the provider changes.
  const [key, setKey] = useState("");
  const { busy, run, setNote, view } = useRun();

  const refresh = async () => {
    const list = await invoke<Provider[]>("list_providers");
    setProviders(list);
    setId((current) => current || list[0]?.id || "");
  };
  useEffect(() => {
    refresh().catch((e) => setNote({ ok: false, text: String(e) }));
  }, []);

  const selected = providers.find((p) => p.id === id);
  const restarted = (h: HostInfo) => {
    setHost(h);
    onHost?.(h);
  };

  const save = () =>
    run(async () => {
      restarted(await invoke<HostInfo>("set_provider_key", { id, key }));
      setKey("");
      await refresh();
      return "Saved in your system keychain.";
    });

  const remove = () =>
    run(async () => {
      restarted(await invoke<HostInfo>("delete_provider_key", { id }));
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
    <section className="card form" aria-label="Provider key">
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
      {view}
    </section>
  );
}

/** Replaces the host's main key, for when it leaked. */
export function MainKey({ onHost }: { onHost: (h: HostInfo) => void }) {
  const { busy, run, view } = useRun();
  const confirm = useConfirm();
  return (
    <section className="card">
      {confirm.dialog}
      <h2>Main API key</h2>
      <p className="muted">
        If the main key leaked, make a new one. Apps using the old key stop working until you give them the new one; keys made on
        the API page keep working.
      </p>
      <div className="actions">
        <button
          className="danger"
          disabled={busy}
          onClick={() =>
            confirm
              .ask({
                title: "Make a new main key?",
                body: "Apps using the current main key stop working until you give them the new one. Keys made on the API page keep working.",
                action: "Make a new key",
              })
              .then(
                (ok) =>
                  void (ok &&
                  run(async () => {
                    onHost(await invoke<HostInfo>("rotate_host_key"));
                    await invoke("open_dashboard"); // the old dashboard login ended with the old key
                    return "Done. The dashboard's API page shows the new key.";
                  })),
              )
          }
        >
          Make a new main key
        </button>
      </div>
      {view}
    </section>
  );
}
