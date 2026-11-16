import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import "../App.css";
import "./dashboard.css";
import { connect, Connection } from "./api";
import Overview from "./Overview";
import Models from "./Models";
import Connect from "./Connect";

const PAGES = { overview: "Overview", models: "Models", connect: "Connect" } as const;
type Page = keyof typeof PAGES;
const fromHash = (): Page => (location.hash.slice(1) in PAGES ? (location.hash.slice(1) as Page) : "overview");

function Dashboard() {
  const [conn, setConn] = useState<Connection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState<Page>(fromHash);

  useEffect(() => {
    connect().then(setConn, (e) => setError(String(e.message ?? e)));
    const onHash = () => setPage(fromHash());
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);

  return (
    <>
      <header className="topbar">
        <strong>Glint Studio</strong>
        <nav aria-label="Dashboard">
          {(Object.keys(PAGES) as Page[]).map((p) => (
            <a key={p} href={`#${p}`} aria-current={p === page ? "page" : undefined}>
              {PAGES[p]}
            </a>
          ))}
        </nav>
      </header>
      <main className="page wide">
        {error && <p className="error">{error}</p>}
        {conn && page === "overview" && <Overview />}
        {conn && page === "models" && <Models />}
        {conn && page === "connect" && <Connect conn={conn} />}
      </main>
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <Dashboard />
  </React.StrictMode>,
);
