import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import "../App.css";
import "./dashboard.css";
import { version } from "../../package.json";
import { api, connect, Connection, System } from "./api";
import { Alert, Icon, IconName } from "./ui";
import Status from "./Overview";
import Models from "./Models";
import Generate from "./Generate";
import Connect from "./Connect";
import Logs from "./Logs";
import Bench from "./Bench";
import Settings from "./Settings";
import { applyAppearance } from "./settings/Appearance";

applyAppearance(); // before the first render, so a forced theme never flashes the system one

const PAGES = {
  dashboard: ["Dashboard", "activity"],
  models: ["Models", "box"],
  connect: ["API", "plug"],
  generate: ["Generate", "image"],
  bench: ["Bench", "gauge"],
  logs: ["Logs", "terminal"],
  settings: ["Settings", "settings"],
} as const satisfies Record<string, [string, IconName]>;
type Page = keyof typeof PAGES;
// Hover menus open a view (Models) or jump to a section (Connect) inside the page: #page/sub.
const MENUS: Partial<Record<Page, [string, string, IconName][]>> = {
  models: [
    ["discover", "Discover", "download"],
    ["installed", "Installed", "check"],
  ],
  connect: [
    ["this-computer", "On this computer", "monitor"],
    ["share", "Share", "globe"],
    ["keys", "Keys", "key"],
    ["apps", "Apps", "sparkles"],
    ["try", "Try it", "terminal"],
  ],
  settings: [
    ["engine", "Engine", "cpu"],
    ["downloads", "Downloads", "download"],
    ["security", "Security", "key"],
    ["usage", "Usage", "activity"],
    ["appearance", "Appearance", "palette"],
  ],
};
const fromHash = () => {
  const [h, sub = ""] = location.hash.slice(1).split("/");
  const p = h === "styles" ? "generate" : h; // Styles now lives inside Generate
  return { page: (p in PAGES ? p : "dashboard") as Page, sub }; // #status and #overview, the old names, land here too
};

/** The engine's state in a word, with a dot that is never the only signal. */
function EngineStatus({ system }: { system: System | null }) {
  if (!system)
    return (
      <div className="engine quiet" role="status">
        <span className="dot" aria-hidden="true" />
        <span>Connecting…</span>
      </div>
    );
  const { up, model } = system.engine;
  const queue = system.queue.running + system.queue.queued;
  const note = queue ? `${queue} in queue` : model?.status === "loading" ? "loading model…" : model?.status === "loaded" ? "model ready" : "idle";
  return (
    <div className={up ? "engine" : "engine bad"} role="status">
      <span className="dot" aria-hidden="true" />
      <span>{up ? "Running" : "Stopped"}</span>
      {up && <span className="num">· {note}</span>}
    </div>
  );
}

function Dashboard() {
  const [conn, setConn] = useState<Connection | null>(null);
  const [system, setSystem] = useState<System | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [{ page, sub }, setRoute] = useState(fromHash);

  useEffect(() => {
    connect().then(setConn, (e) => setError(String(e.message ?? e)));
    const onHash = () => setRoute(fromHash());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (!conn) return;
    const load = () => api<System>("/api/v1/system").then(setSystem, () => setSystem(null));
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [conn]);

  useEffect(() => {
    document.title = `${PAGES[page][0]} · Glint Studio`;
    // On narrow screens the nav scrolls sideways; keep the current page in view.
    document.querySelector('.nav [aria-current="page"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [page]);

  // A section link scrolls once the section has rendered; Connect draws some only after its first fetch.
  useEffect(() => {
    if (!conn || !sub || page === "models") return;
    let raf = 0;
    let tries = 0;
    const go = () => {
      const el = document.getElementById(sub);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      else if (tries++ < 120) raf = requestAnimationFrame(go);
    };
    go();
    return () => cancelAnimationFrame(raf);
  }, [conn, page, sub]);

  return (
    <>
      <header className="topbar">
        <div className="topbar-in">
          <a className="brand" href="#dashboard">
            <span className="mark" aria-hidden="true" />
            <span>
              Glint Studio
              <small className="num">v{version}</small>
            </span>
          </a>
          <nav aria-label="Main">
            <ul className="nav">
              {(Object.keys(PAGES) as Page[]).map((p) => (
                <li key={p} className="nav-item">
                  <a href={`#${p}`} aria-current={p === page ? "page" : undefined} aria-label={PAGES[p][0]} title={PAGES[p][0]}>
                    <Icon name={PAGES[p][1]} />
                    <span className="nav-label">{PAGES[p][0]}</span>
                  </a>
                  {MENUS[p] && (
                    <div className="nav-menu">
                      <ul>
                        {MENUS[p].map(([id, label, icon]) => (
                          <li key={id}>
                            <a href={`#${p}/${id}`} aria-current={p === page && id === sub ? "location" : undefined}>
                              <Icon name={icon} />
                              {label}
                            </a>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </nav>
          <EngineStatus system={system} />
        </div>
      </header>
      <main className="main">
        <div className="content">
          {error && <Alert>{error}</Alert>}
          {conn && page === "dashboard" && <Status system={system} />}
          {conn && page === "generate" && <Generate />}
          {conn && page === "models" && <Models tab={sub === "installed" ? "installed" : "discover"} system={system} />}
          {conn && page === "connect" && <Connect conn={conn} />}
          {conn && page === "bench" && <Bench />}
          {conn && page === "logs" && <Logs />}
          {conn && page === "settings" && <Settings sub={sub} />}
        </div>
      </main>
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <Dashboard />
  </React.StrictMode>,
);
