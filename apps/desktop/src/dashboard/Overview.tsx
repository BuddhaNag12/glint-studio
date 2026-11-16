import { useEffect, useState } from "react";
import { api, gb, JobEvent, onJobEvents, secs, Stats, System } from "./api";

const DAYS = 14;
const dayLabel = (day: number) =>
  new Date(day * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });

export default function Overview() {
  const [system, setSystem] = useState<System | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [jobs, setJobs] = useState<Record<string, JobEvent>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadSystem = () => api<System>("/api/v1/system").then(setSystem, (e) => setError(e.message));
    const loadStats = () => api<Stats>(`/api/v1/stats?days=${DAYS}`).then(setStats, (e) => setError(e.message));
    loadSystem();
    loadStats();
    const timer = setInterval(loadSystem, 3000);
    const stop = onJobEvents((e) => {
      setJobs((all) => ({ ...all, [e.id]: e }));
      if (["done", "failed", "cancelled"].includes(e.event)) loadStats();
    });
    return () => {
      clearInterval(timer);
      stop();
    };
  }, []);

  const recent = Object.values(jobs).sort((a, b) => b.created - a.created).slice(0, 6);
  const t = stats?.totals;

  return (
    <>
      <h1>Overview</h1>
      {error && <p className="error">{error}</p>}

      <div className="tiles">
        <Tile label="Engine" value={system ? (system.engine.up ? "● Running" : "○ Stopped") : "…"} tone={system && !system.engine.up ? "bad" : undefined} />
        <Tile label="Queue" value={system ? `${system.queue.running} running · ${system.queue.queued} waiting` : "…"} />
        <Tile label={`Images, last ${DAYS} days`} value={t ? String(t.done) : "…"} />
        <Tile label="Failed" value={t ? String(t.failed) : "…"} tone={t?.failed ? "bad" : undefined} />
      </div>

      {system && !system.engine.up && <p className="error">{system.engine.error}</p>}

      <section className="card">
        <h2>Images per day</h2>
        {stats && <DayChart days={stats.days} />}
      </section>

      <div className="split">
        <section className="card">
          <h2>Machine</h2>
          {system ? (
            <dl className="facts">
              {system.engine.devices.map((d) => (
                <GpuMemory key={d.name} name={d.name} total={d.vram_total} free={d.vram_free} />
              ))}
              <dt>Backend</dt>
              <dd>{system.hardware.backend.toUpperCase()}</dd>
              <dt>Models folder</dt>
              <dd>{gb(system.models_bytes)}</dd>
              <dt>Free disk</dt>
              <dd>{system.hardware.disk_free_gb.toFixed(0)} GB</dd>
            </dl>
          ) : (
            <p className="lead">…</p>
          )}
        </section>

        <section className="card">
          <h2>Live jobs</h2>
          {recent.length === 0 ? (
            <p className="lead">Jobs show up here as apps send them.</p>
          ) : (
            <ul className="jobs">
              {recent.map((j) => (
                <li key={j.id}>
                  <span>{j.model}</span>
                  {j.status === "running" ? (
                    <progress value={j.progress} max={1} aria-label={`${j.model} progress`} />
                  ) : (
                    <span className={j.status === "failed" ? "error" : "muted"}>
                      {j.status === "queued" ? `waiting${j.queue_position ? ` (#${j.queue_position})` : ""}` : j.status}
                      {j.status === "done" && ` · ${secs(j.elapsed_ms)}`}
                      {j.error && ` · ${j.error.code}`}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {stats && stats.models.length > 0 && (
        <section className="card">
          <h2>Models used</h2>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Model</th>
                <th scope="col">Jobs</th>
                <th scope="col">Typical time</th>
                <th scope="col">Slowest 5%</th>
              </tr>
            </thead>
            <tbody>
              {stats.models.map((m) => (
                <tr key={m.model}>
                  <td>{m.model}</td>
                  <td>{m.jobs}</td>
                  <td>{secs(m.p50_ms)}</td>
                  <td>{secs(m.p95_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {stats && (stats.errors.length > 0 || stats.styles.length > 0) && (
        <div className="split">
          <section className="card">
            <h2>Errors</h2>
            <Counts rows={stats.errors.map((e) => [e.code, e.count])} empty="No errors." />
          </section>
          <section className="card">
            <h2>Styles</h2>
            <Counts rows={stats.styles.map((s) => [s.style, s.count])} empty="No styles used yet." />
          </section>
        </div>
      )}
    </>
  );
}

function Tile({ label, value, tone }: { label: string; value: string; tone?: "bad" }) {
  return (
    <div className="card tile">
      <span className="muted">{label}</span>
      <strong className={tone === "bad" ? "error" : undefined}>{value}</strong>
    </div>
  );
}

/** A GPU's memory as a meter: used of total. */
function GpuMemory({ name, total, free }: { name: string; total: number; free: number }) {
  return (
    <>
      <dt>{name.replace(/^cuda:\d+\s*/, "").split(" : ")[0] || name}</dt>
      <dd>
        <meter min={0} max={total} value={total - free} aria-label="GPU memory in use" />{" "}
        {gb(total - free)} of {gb(total)} in use
      </dd>
    </>
  );
}

function Counts({ rows, empty }: { rows: [string, number][]; empty: string }) {
  if (rows.length === 0) return <p className="lead">{empty}</p>;
  return (
    <table className="data">
      <tbody>
        {rows.map(([k, n]) => (
          <tr key={k}>
            <td>{k}</td>
            <td>{n}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Stacked bars, done under failed; hover a day for its numbers, or open the table. */
function DayChart({ days }: { days: Stats["days"] }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 640, H = 180, top = 8, bottom = 24, left = 28;
  const max = Math.max(1, ...days.map((d) => d.done + d.failed));
  const step = Math.max(1, Math.ceil(max / 4));
  const ticks = Array.from({ length: Math.floor(max / step) + 1 }, (_, i) => i * step);
  const yMax = ticks[ticks.length - 1] < max ? max : ticks[ticks.length - 1];
  const y = (v: number) => top + (H - top - bottom) * (1 - v / yMax);
  const slot = (W - left) / days.length;
  const bw = Math.min(28, slot * 0.6);
  const gap = 2;

  return (
    <div className="chart">
      <div className="legend" aria-hidden="true">
        <span><i className="swatch done" /> Done</span>
        <span><i className="swatch failed" /> Failed</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Images per day for the last ${days.length} days`} onMouseLeave={() => setHover(null)}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={left} x2={W} y1={y(v)} y2={y(v)} className="grid" />
            <text x={left - 6} y={y(v) + 4} textAnchor="end" className="axis">{v}</text>
          </g>
        ))}
        {days.map((d, i) => {
          const x = left + i * slot + (slot - bw) / 2;
          const doneTop = y(d.done);
          const failedTop = y(d.done + d.failed);
          return (
            <g key={d.day}>
              {d.done > 0 && <path d={bar(x, doneTop, bw, y(0) - doneTop, d.failed === 0)} className="done" />}
              {d.failed > 0 && (
                <path d={bar(x, failedTop, bw, doneTop - failedTop - (d.done > 0 ? gap : 0), true)} className="failed" />
              )}
              {(days.length - 1 - i) % Math.ceil(days.length / 7) === 0 && (
                <text x={x + bw / 2} y={H - 6} textAnchor="middle" className="axis">{dayLabel(d.day)}</text>
              )}
              <rect x={left + i * slot} y={top} width={slot} height={H - top - bottom} className="hit" onMouseEnter={() => setHover(i)} />
            </g>
          );
        })}
      </svg>
      {hover !== null && (
        <div className="tooltip" style={{ left: `${((left + (hover + 0.5) * slot) / W) * 100}%` }}>
          <strong>{dayLabel(days[hover].day)}</strong>
          <span>{days[hover].done} done</span>
          <span>{days[hover].failed} failed</span>
        </div>
      )}
      <details>
        <summary>Show as a table</summary>
        <table className="data">
          <thead>
            <tr><th scope="col">Day</th><th scope="col">Done</th><th scope="col">Failed</th></tr>
          </thead>
          <tbody>
            {days.map((d) => (
              <tr key={d.day}><td>{dayLabel(d.day)}</td><td>{d.done}</td><td>{d.failed}</td></tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

/** A bar from the baseline up; the top corners are rounded (4px) only on the topmost segment. */
function bar(x: number, yTop: number, w: number, h: number, roundTop: boolean) {
  if (h <= 0) return "";
  const r = roundTop ? Math.min(4, w / 2, h) : 0;
  return `M${x},${yTop + h}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + w - r}Q${x + w},${yTop} ${x + w},${yTop + r}V${yTop + h}Z`;
}
