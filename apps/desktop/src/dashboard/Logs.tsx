import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { Alert, CopyButton, PageHeader, Tabs } from "./ui";

type LogFile = "host" | "comfyui" | "tunnel";
type Level = "debug" | "info" | "warning" | "error";
const FILES: Record<LogFile, string> = { host: "Host", comfyui: "Engine", tunnel: "Public link" };
const RANK: Record<Level, number> = { debug: 0, info: 1, warning: 2, error: 3 };
const SHOW: [Level, string][] = [["debug", "All"], ["info", "Info"], ["warning", "Warnings"], ["error", "Errors"]];
const BADGE: Record<Level, string> = { debug: "Debug", info: "Info", warning: "Warn", error: "Error" };
const COUNTS = [500, 1000, 2000, 5000];
const EVERY: [number, string][] = [[0, "Off"], [2, "2 s"], [5, "5 s"], [10, "10 s"]];

// ComfyUI colors its level words; the colors would show as junk.
const ANSI = /\x1b\[[0-9;]*m/g;
// "2026-11-16T15:34:47Z", "2026-11-16 15:34:47,123" or "[2026-11-16 15:34:47.123]" at the start of a line.
const TIME = /^\[?(\d{4}-\d\d-\d\d[T ][\d:.,]+Z?)\]?\s+/;
// Python's "LEVEL:module:msg" (uvicorn's "INFO:     msg" has no module), ComfyUI's "[INFO] msg", and
// cloudflared's "INF msg" after its time.
const LEVEL = /^(?:(DEBUG|INFO|WARNING|ERROR|CRITICAL):(?:([\w.]+):)?|\[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]|(DBG|INF|WRN|ERR|FTL)\b)\s*/;
const WORD: Record<string, Level> = {
  DEBUG: "debug", DBG: "debug", INFO: "info", INF: "info", WARNING: "warning", WRN: "warning",
  ERROR: "error", ERR: "error", CRITICAL: "error", FTL: "error",
};

type Row = { text: string; time: string; module: string; message: string; level: Level; own: boolean };

/** Splits each line into time, level, module and message; a part it can't find stays blank and the rest is
 * the message. A line without a level (a traceback's body, a wrapped message) keeps the one before it. */
function parse(lines: string[]): Row[] {
  let last: Level = "info";
  return lines.map((raw) => {
    const text = raw.replace(ANSI, "");
    const t = TIME.exec(text);
    const rest = t ? text.slice(t[0].length) : text;
    const m = LEVEL.exec(rest);
    const word = m && (m[1] ?? m[3] ?? (t ? m[4] : undefined)); // a bare "ERR" without a time is just text
    const own = !!word || text.startsWith("Traceback ");
    if (own) last = word ? WORD[word] : "error";
    return { text, time: t?.[1] ?? "", module: (word && m[2]) || "", message: word ? rest.slice(m[0].length) : rest, level: last, own };
  });
}

export default function Logs() {
  const [file, setFile] = useState<LogFile>("host");
  const [files, setFiles] = useState<LogFile[]>([]);
  const [lines, setLines] = useState<string[] | null>(null);
  const [total, setTotal] = useState(0);
  const [count, setCount] = useState(2000);
  const [every, setEvery] = useState(2);
  const [view, setView] = useState<"table" | "raw">("table");
  const [min, setMin] = useState<Level>("debug");
  const [find, setFind] = useState("");
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const atEnd = useRef(true);

  useEffect(() => {
    let on = true; // a reply for the file or count just left never lands
    const load = () =>
      api<{ files: LogFile[]; lines: string[]; total: number }>(`/api/v1/logs?file=${file}&lines=${count}`).then(
        (r) => {
          if (!on) return;
          setFiles(r.files);
          setTotal(r.total);
          // Same lines: keep the old array so 5000 rows aren't drawn again every refresh.
          setLines((old) => (old && old.length === r.lines.length && old.every((x, i) => x === r.lines[i]) ? old : r.lines));
          setError(null);
        },
        (e) => on && setError(e.message),
      );
    load();
    const timer = every ? setInterval(load, every * 1000) : undefined;
    return () => {
      on = false;
      clearInterval(timer);
    };
  }, [file, count, every]);

  const pick = (f: LogFile) => {
    setFile(f);
    setLines(null);
    atEnd.current = true;
  };

  const shown = useMemo(() => {
    const q = find.trim().toLowerCase();
    return parse(lines ?? []).filter((l) => RANK[l.level] >= RANK[min] && (!q || l.text.toLowerCase().includes(q)));
  }, [lines, min, find]);

  // Follow new lines while scrolled to the end; stay put while reading further up.
  useLayoutEffect(() => {
    const el = box.current;
    if (el && atEnd.current) el.scrollTop = el.scrollHeight;
  }, [shown, view]);

  const tabs = (Object.keys(FILES) as LogFile[]).filter((f) => f === file || files.includes(f));

  return (
    <>
      <PageHeader title="Logs" lead="What this host, the engine and the public link are doing, newest at the bottom">
        <div className="usage-filters">
          <label className="inline-field">
            Lines
            <select value={count} onChange={(e) => setCount(Number(e.target.value))}>
              {COUNTS.map((n) => (
                <option key={n} value={n}>
                  {n.toLocaleString()}
                </option>
              ))}
            </select>
          </label>
          <label className="inline-field">
            Refresh
            <select value={every} onChange={(e) => setEvery(Number(e.target.value))}>
              {EVERY.map(([s, text]) => (
                <option key={s} value={s}>
                  {text}
                </option>
              ))}
            </select>
          </label>
        </div>
      </PageHeader>
      <section className="card logs" aria-label="Log">
        <div className="logs-bar">
          {tabs.length > 1 && <Tabs label="Log file" value={file} onChange={pick} options={tabs.map((f) => [f, FILES[f]])} />}
          <Tabs label="Show" value={min} onChange={setMin} options={SHOW} />
          <Tabs label="View" value={view} onChange={setView} options={[["table", "Table"], ["raw", "Raw"]]} />
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
          ) : view === "raw" ? (
            <pre>{shown.map((l) => l.text).join("\n")}</pre>
          ) : (
            <table>
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Level</th>
                  <th scope="col">Module</th>
                  <th scope="col">Message</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((l, i) => (
                  <tr key={i} className={`line ${l.level}`}>
                    <td className="time">{l.time}</td>
                    <td className="lvl">{l.own ? BADGE[l.level] : ""}</td>
                    <td className="mod">{l.module}</td>
                    <td className="msg">{l.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <p className="count" aria-live="off">
          {lines && `Showing ${shown.length.toLocaleString()} of ${total.toLocaleString()} lines`}
        </p>
      </section>
    </>
  );
}
