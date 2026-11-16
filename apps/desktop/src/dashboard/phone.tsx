import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import "../App.css";
import "./dashboard.css";
import { api, setKey } from "./api";
import Generate from "./Generate";

const STORE = "glint.key";
const PAIR = "Scan the QR code on Glint Studio's Connect page (Pair a phone) to use this page.";

/** The QR code opens /m#key=…: keep the key on this phone and take it out of the address bar. The fragment
 * never reaches the host or its log. */
function pairedKey(): string | null {
  const fromQr = location.hash.match(/key=([\w-]+)/)?.[1] ?? null;
  if (fromQr) history.replaceState(null, "", location.pathname);
  try {
    if (fromQr) localStorage.setItem(STORE, fromQr);
    return localStorage.getItem(STORE) ?? fromQr;
  } catch {
    return fromQr; // private browsing: works until the page is closed
  }
}

function Phone() {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const key = pairedKey();
    if (!key) return setError(PAIR);
    setKey(key);
    api("/api/v1/models").then(() => setReady(true), (e) => setError(`${e.message} ${PAIR}`));
  }, []);

  return (
    <>
      <header className="topbar">
        <strong>Glint Studio</strong>
      </header>
      <main className="page wide">
        {error && <p className="error" role="alert">{error}</p>}
        {ready && <Generate />}
      </main>
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <Phone />
  </React.StrictMode>,
);
