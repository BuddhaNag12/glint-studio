import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { Alert, CopyButton, PageHeader, Tabs } from "./ui";

type LogFile = "host" | "comfyui" | "tunnel";
type Level = "debug" | "info" | "warning" | "error";
const FILES: Record<LogFile, string> = { host: "Host", comfyui: "Engine", tunnel: "Public link" };
const RANK: Record<Level, number> = { debug: 0, info: 1, warning: 2, error: 3 };
const SHOW: [Level, string][] = [["debug", "All"], ["info", "Info"], ["warning", "Warnings"], ["error", "Errors"]];
const BADGE: Record<Level, string> = { debug: "Debug", info: "Info", warning: "Warn", error: "Error" };

// uvicorn writes "INFO:     msg", cloudflared "2026-11-16T15:34:47Z INF msg"; ComfyUI mostly plain prints.
const LEVEL = /^(DEBUG|INFO|WARNING|ERROR|CRITICAL):|^\S+Z (DBG|INF|WRN|ERR|FTL) /;
const WORD: Record<string, Level> = {
  DEBUG: "debug", DBG: "debug", INFO: "info", INF: "info", WARNING: "warning", WRN: "warning",
  ERROR: "error", ERR: "error", CRITICAL: "error", FTL: "error",
};

/** Each line's level, and its text without the level word the badge now shows. A line without a level
 * (a traceback's body, a wrapped message) keeps the one before it. */
function levels(lines: string[]): { text: string; body: string; level: Level; own: boolean }[] {
  let last: Level = "info";
  return lines.map((text) => {
    const m = LEVEL.exec(text);
    const own = !!m || text.startsWith("Traceback ");
    if (own) last = m ? WORD[m[1] ?? m[2]] : "error";
    const rest = m ? text.slice(m[0].length) : text;
    const body = !m ? text : m[1] ? rest.trimStart() : `${m[0].split(" ")[0]} ${rest}`;
    return { text, body, level: last, own };
  });
}

export default function Logs() {
  const [file, setFile] = useState<LogFile>("host");
  const [files, setFiles] = useState<LogFile[]>([]);
  const [lines, setLines] = useState<string[] | null>(null);
  const [min, setMin] = useState<Level>("debug");
  const [find, setFind] = useState("");
  const [live, setLive] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const atEnd = useRef(true);

  useEffect(() => {
    setLines(null);
    atEnd.current = true;
    const load = () =>
      api<{ files: LogFile[]; lines: string[] }>(`/api/v1/logs?file=${file}&lines=2000`).then(
        (r) => {
          setFiles(r.files);
          setLines(r.lines);
          setError(null);
        },
        (e) => setError(e.message),
      );
    load();
    if (!live) return;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [file, live]);

  const shown = useMemo(() => {
    const q = find.trim().toLowerCase();
    return levels(lines ?? []).filter((l) => RANK[l.level] >= RANK[min] && (!q || l.text.toLowerCase().includes(q)));
  }, [lines, min, find]);

  // Follow new lines while scrolled to the end; stay put while reading further up.
  useLayoutEffect(() => {
    const el = box.current;
    if (el && atEnd.current) el.scrollTop = el.scrollHeight;
  }, [shown]);

  const tabs = (Object.keys(FILES) as LogFile[]).filter((f) => f === file || files.includes(f));

  return (
    <>
      <PageHeader title="Logs" lead="What this host, the engine and the public link are doing, newest at the bottom">
        <label className="toggle">
          <input type="checkbox" role="switch" className="switch" checked={live} onChange={(e) => setLive(e.target.checked)} />
          {live ? "Live" : "Paused"}
        </label>
      </PageHeader>
      <section className="card logs" aria-label="Log">
        <div className="logs-bar">
          {tabs.length > 1 && <Tabs label="Log file" value={file} onChange={setFile} options={tabs.map((f) => [f, FILES[f]])} />}
          <Tabs label="Show" value={min} onChange={setMin} options={SHOW} />
          <input type="search" aria-label="Filter lines" placeholder="Filter" value={find} onChange={(e) => setFind(e.target.value)} />
          <CopyButton text={shown.map((l) => l.text).join("\n")} label="shown lines" />
        </div>
        {error && <Alert>{error}</Alert>}
        <div
          className="log"
          ref={box}
          tabIndex={0}
          role="log"
          aria-label={`${FILES[file]} log`}
          onScroll={(e) => {
            const el = e.currentTarget;
            atEnd.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          }}
        >
          {lines === null ? (
            !error && <p className="empty">Loading…</p>
          ) : shown.length === 0 ? (
            <p className="empty">{lines.length ? "No lines match." : "Nothing logged yet."}</p>
          ) : (
            shown.map((l, i) => (
              <div key={i} className={`line ${l.level}`}>
                <span className="lvl">{l.own ? BADGE[l.level] : ""}</span>
                <code>{l.body}</code>
              </div>
            ))
          )}
        </div>
        <p className="count" aria-live="off">
          {lines && `${shown.length} of ${lines.length} lines`}
        </p>
      </section>
    </>
  );
}
