import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Alert, Icon } from "../dashboard/ui";

// Mirrors HardwareProfile in src-tauri/src/scan.rs.
type Backend = "cuda" | "mps" | "rocm" | "xpu" | "cpu";
type Gpu = { vendor: string; name: string; vram_gb: number | null; driver: string | null };
type HardwareProfile = {
  os: string;
  arch: string;
  cpu: string;
  ram_gb: number;
  disk_free_gb: number;
  gpus: Gpu[];
  backend: Backend;
  vram_budget_gb: number;
  warnings: string[];
};
export type Engine = "local" | "cloud";

const BACKEND_LABEL: Record<Backend, string> = {
  cuda: "NVIDIA GPU (CUDA)",
  mps: "Apple Silicon GPU (Metal)",
  rocm: "AMD GPU (ROCm)",
  xpu: "Intel Arc GPU",
  cpu: "CPU only",
};
const OS_LABEL: Record<string, string> = { macos: "macOS", windows: "Windows", linux: "Linux" };

/** The first step: what Glint Studio is and what setup will do. */
export function Welcome({ onStart }: { onStart: () => void }) {
  return (
    <main className="page">
      <header className="hero welcome">
        <span className="mark big" aria-hidden="true" />
        <h1>Welcome to Glint Studio</h1>
        <p className="lead">
          Make images with open models on this computer, or with a cloud provider you choose. Other apps can use it too, through one
          local address.
        </p>
        <p className="cap">Open source · GPL-3.0 · macOS, Windows and Linux</p>
      </header>

      <ol className="next" aria-label="What setup does">
        <li>
          <Icon name="cpu" size={20} />
          <span>
            <strong>Check this computer</strong>
            A quick scan shows what it can run.
          </span>
        </li>
        <li>
          <Icon name="download" size={20} />
          <span>
            <strong>Install once</strong>
            Python, the host and, if you want it, the image engine. In a folder of their own, with no admin rights.
          </span>
        </li>
        <li>
          <Icon name="sparkles" size={20} />
          <span>
            <strong>Make images</strong>
            Pick a model in the dashboard. Settings stay here, a click away in the menu bar.
          </span>
        </li>
      </ol>

      <footer className="page-foot">
        <span />
        <button className="primary" onClick={onStart} autoFocus>
          Get started
        </button>
      </footer>
    </main>
  );
}

export default function Onboarding({
  initial,
  onBack,
  onChoose,
}: {
  initial: Engine | null;
  onBack: () => void;
  onChoose: (engine: Engine) => void;
}) {
  const [profile, setProfile] = useState<HardwareProfile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pick, setPick] = useState<Engine | null>(initial);

  async function scan() {
    setProfile(null);
    setError(null);
    try {
      setProfile(await invoke<HardwareProfile>("scan_hardware"));
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => {
    scan();
  }, []);

  const gpuReady = profile !== null && profile.backend !== "cpu" && profile.vram_budget_gb > 0;
  useEffect(() => {
    if (profile) setPick((p) => p ?? (gpuReady ? "local" : "cloud")); // preselect the recommendation
  }, [profile]);

  const choice = (engine: Engine, recommended: boolean, icon: "monitor" | "globe", title: string, body: string) => (
    <button className="choice" aria-pressed={pick === engine} onClick={() => setPick(engine)}>
      <span className="choice-head">
        <Icon name={icon} size={20} />
        <span className="radio" aria-hidden="true" />
      </span>
      <strong>
        {title}
        {recommended && <span className="tag">Recommended</span>}
      </strong>
      <span>{body}</span>
    </button>
  );

  return (
    <main className="page">
      <header className="hero">
        <h1>This computer</h1>
        <p className="lead">A quick look at this computer shows what it can run. Then pick where your images are made.</p>
      </header>

      {profile && (
        <section className="choices" aria-labelledby="how-title">
          <h2 id="how-title">How do you want to make images?</h2>
          {choice(
            "local",
            gpuReady,
            "monitor",
            "On this computer",
            `Free and private. Installs the image engine (a few GB) and a model that fits.${gpuReady ? "" : " Without a supported GPU this is slow."}`,
          )}
          {choice("cloud", !gpuReady, "globe", "With a cloud provider", "Fast on any computer. Uses your own API key, billed by the provider. Small install.")}
        </section>
      )}

      <section className="card" aria-live="polite" aria-busy={!profile && !error} aria-labelledby="computer-title">
        <h2 id="computer-title">What it has</h2>
        {error ? (
          <>
            <Alert>The scan didn't work: {error}</Alert>
            <div className="actions">
              <button onClick={scan}>Try again</button>
            </div>
          </>
        ) : !profile ? (
          <p className="status quiet">
            <Icon name="spinner" size={16} />
            Scanning your hardware…
          </p>
        ) : (
          <>
            <dl className="facts">
              <dt>System</dt>
              <dd>
                {OS_LABEL[profile.os] ?? profile.os} ({profile.arch})
              </dd>
              <dt>Processor</dt>
              <dd>{profile.cpu || "Unknown"}</dd>
              <dt>Memory</dt>
              <dd className="num">{profile.ram_gb} GB</dd>
              <dt>Free disk</dt>
              <dd className="num">{profile.disk_free_gb} GB</dd>
              <dt>Graphics</dt>
              <dd>
                {profile.gpus.length === 0
                  ? "None found"
                  : profile.gpus.map((g) => (g.vram_gb ? `${g.name}, ${g.vram_gb} GB` : g.name)).join("; ")}
              </dd>
              <dt>Runs models on</dt>
              <dd>
                {BACKEND_LABEL[profile.backend]}
                {profile.vram_budget_gb > 0 && `, up to ${profile.vram_budget_gb} GB for models`}
              </dd>
            </dl>
            {profile.warnings.map((w) => (
              <Alert key={w} info>
                {w}
              </Alert>
            ))}
          </>
        )}
      </section>

      <footer className="page-foot">
        <button className="ghost" onClick={onBack}>
          Back
        </button>
        <button className="primary" disabled={!pick} onClick={() => pick && onChoose(pick)}>
          Continue
        </button>
      </footer>
    </main>
  );
}
