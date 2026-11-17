import { ReactNode, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { version } from "../../package.json";
import { api, setKey, System } from "../dashboard/api";
import { Alert, Icon, IconName } from "../dashboard/ui";
import ServerCard from "../dashboard/ServerCard";
import Engine from "../dashboard/settings/Engine";
import Downloads from "../dashboard/settings/Downloads";
import Security from "../dashboard/settings/Security";
import Usage from "../dashboard/settings/Usage";
import Appearance from "../dashboard/settings/Appearance";
import { MainKey, ProviderKeys } from "./Providers";
import type { HostInfo } from "./Setup";

type Pane = "status" | "engine" | "downloads" | "providers" | "appearance" | "security" | "usage" | "about";
const GROUPS: [string, [Pane, string, IconName, string][]][] = [
  ["Server", [["status", "Status", "activity", "The host and the image engine on this computer."]]],
  [
    "Engine",
    [
      ["engine", "Engine", "cpu", "When a model loads and unloads, and how much memory the engine keeps free."],
      ["downloads", "Downloads", "download", "Where models download from."],
      ["providers", "Cloud providers", "globe", "Your own API keys, kept in the system keychain and only sent to the provider."],
    ],
  ],
  ["App", [["appearance", "Appearance", "palette", "Theme and text size, here and in the dashboard."]]],
  [
    "General",
    [
      ["security", "Security", "key", "Who on your network can use the host, and the main API key."],
      ["usage", "Usage", "gauge", "Whether usage is recorded, and the history so far."],
      ["about", "About", "sparkles", ""],
    ],
  ],
];
const PANES = GROUPS.flatMap(([, panes]) => panes);

// The dashboard's api() fetches paths like /api/v1/system from the page the host serves; in this window they go to the host.
let hostUrl = "";
const fetchHere = window.fetch.bind(window);
window.fetch = (input, init) => fetchHere(typeof input === "string" && input.startsWith("/api/") ? hostUrl + input : input, init);

/** The app window once setup has run: a sidebar of settings panes, like a system settings window. */
export default function Settings({ onSetup }: { onSetup: () => void }) {
  const [pane, setPane] = useState<Pane>("status");
  const [host, setHost] = useState<HostInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [system, setSystem] = useState<System | null>(null);

  const hostChanged = (h: HostInfo) => {
    hostUrl = h.url;
    setKey(h.api_key);
    setHost(h);
  };
  const start = () => {
    setError(null);
    invoke<HostInfo>("start_host").then(hostChanged, (e) => setError(String(e)));
  };
  useEffect(start, []);

  // The menu bar's About item.
  useEffect(() => {
    const off = listen<Pane>("pane", (e) => setPane(e.payload));
    return () => void off.then((f) => f());
  }, []);

  // Only the Status pane shows live numbers.
  useEffect(() => {
    if (!host || pane !== "status") return;
    const load = () => api<System>("/api/v1/system").then(setSystem, () => setSystem(null));
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [host, pane]);

  // The shared cards link to dashboard pages (#models/discover): open those in the browser instead.
  useEffect(() => {
    const onHash = () => {
      const page = location.hash.slice(1);
      if (!page) return;
      history.replaceState(null, "", location.pathname);
      invoke("open_dashboard", { page }).catch((e) => setError(String(e)));
    };
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);

  const [, title, , lead] = PANES.find(([id]) => id === pane)!;
  const needsHost = pane !== "appearance" && pane !== "about";
  const dashboard = (page?: string) => invoke("open_dashboard", { page }).catch((e) => setError(String(e)));

  const content: Record<Pane, () => ReactNode> = {
    status: () => (
      <>
        <ServerCard system={system} />
        <section className="card card-head">
          <div>
            <h2>Dashboard</h2>
            <p className="muted">Make images, add models and connect other apps, in your browser.</p>
          </div>
          <div className="actions">
            <button onClick={() => dashboard("models/discover")}>Find models</button>
            <button className="primary" onClick={() => dashboard()}>
              Open the dashboard
            </button>
          </div>
        </section>
      </>
    ),
    engine: () => <Engine />,
    downloads: () => <Downloads />,
    providers: () => host && <ProviderKeys host={host} onHost={hostChanged} />,
    appearance: () => <Appearance />,
    security: () => (
      <>
        <Security />
        <MainKey onHost={hostChanged} />
      </>
    ),
    usage: () => <Usage />,
    about: () => (
      <>
        <section className="card about">
          <span className="mark big" aria-hidden="true" />
          <div>
            <h2>Glint Studio</h2>
            <p className="muted">Open-source image generation on your own computer, or with a cloud provider you choose.</p>
          </div>
          <dl className="facts">
            <dt>Version</dt>
            <dd className="num">{version}</dd>
            <dt>License</dt>
            <dd>GPL-3.0</dd>
            <dt>Host</dt>
            <dd>{host ? <code>{host.url}</code> : "Not running"}</dd>
          </dl>
        </section>
        <section className="card card-head">
          <div>
            <h2>Setup</h2>
            <p className="muted">Run the setup steps again to repair the install, or to switch between this computer and a cloud provider.</p>
          </div>
          <button onClick={onSetup}>Run setup again</button>
        </section>
      </>
    ),
  };

  return (
    <div className="window">
      <aside className="rail">
        <div className="brand">
          <span className="mark" aria-hidden="true" />
          <span>
            Glint Studio
            <small className="num">v{version}</small>
          </span>
        </div>
        <nav aria-label="Settings">
          {GROUPS.map(([group, panes]) => (
            <div key={group} className="nav-group">
              <h2>{group}</h2>
              <ul>
                {panes.map(([id, label, icon]) => (
                  <li key={id}>
                    <button aria-current={id === pane ? "page" : undefined} onClick={() => setPane(id)}>
                      <Icon name={icon} size={16} />
                      {label}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>
      </aside>
      <main className="page pane" key={pane}>
        <header className="hero">
          <h1>{title}</h1>
          {lead && <p className="lead">{lead}</p>}
        </header>
        {error && (
          <Alert>
            <span>The host isn't running: {error}</span>
            <span className="actions">
              <button className="small" onClick={start}>
                Try again
              </button>
              <button className="small" onClick={onSetup}>
                Run setup again
              </button>
            </span>
          </Alert>
        )}
        {needsHost && !host && !error ? (
          <p className="status quiet" role="status">
            <Icon name="spinner" size={16} />
            Starting the host…
          </p>
        ) : (
          (!needsHost || host) && content[pane]()
        )}
      </main>
    </div>
  );
}
