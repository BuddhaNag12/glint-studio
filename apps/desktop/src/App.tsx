import { useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import Onboarding, { Welcome, type Engine } from "./pages/Onboarding";
import Setup, { Ready, type Installed } from "./pages/Setup";
import Settings from "./pages/Settings";
import { Icon } from "./dashboard/ui";
import { version } from "../package.json";
import "./App.css";
import "./dashboard/dashboard.css";

type Step = "welcome" | "computer" | "install" | "ready";

/** Setup as a stepper across the top: Welcome, This computer, Install, then Ready (or the cloud key). */
function Wizard({ first, onSettings }: { first: boolean; onSettings: () => void }) {
  const [step, setStep] = useState<Step>(first ? "welcome" : "computer");
  const [engine, setEngine] = useState<Engine | null>(null);
  const [done, setDone] = useState<Installed | null>(null);
  const steps: [Step, string][] = [
    ["welcome", "Welcome"],
    ["computer", "This computer"],
    ["install", "Install"],
    ["ready", engine === "cloud" ? "API key" : "Ready"],
  ];
  const at = steps.findIndex(([id]) => id === step);
  return (
    <div className="wizard">
      <header className="wiz-head">
        <div className="brand">
          <span className="mark" aria-hidden="true" />
          <span>Glint Studio</span>
        </div>
        <ol className="stepper" aria-label="Setup steps">
          {steps.map(([id, label], i) => (
            <li key={id} className={i < at ? "done" : undefined} aria-current={i === at ? "step" : undefined}>
              <span className={i < at ? "badge done" : i === at ? "badge now" : "badge"}>
                {i < at ? <Icon name="check" size={14} label="Done:" /> : i + 1}
              </span>
              <span>{label}</span>
            </li>
          ))}
        </ol>
        <span className="cap num">v{version}</span>
      </header>
      {step === "welcome" ? (
        <Welcome onStart={() => setStep("computer")} />
      ) : step === "computer" ? (
        <Onboarding
          initial={engine}
          onBack={first ? () => setStep("welcome") : onSettings}
          onChoose={(e) => {
            setEngine(e);
            setStep("install");
          }}
        />
      ) : step === "install" || !done ? (
        <Setup
          engine={engine!}
          onBack={() => setStep("computer")}
          onReady={(d) => {
            setDone(d);
            setStep("ready");
          }}
        />
      ) : (
        <Ready done={done} engine={engine!} onSettings={onSettings} />
      )}
    </div>
  );
}

export default function App() {
  // null until Rust says whether setup has run: then the window is Settings, else the setup steps.
  const [mode, setMode] = useState<"first" | "again" | "settings" | null>(null);
  useEffect(() => {
    invoke<boolean>("setup_done").then(
      (installed) => setMode(installed ? "settings" : "first"),
      () => setMode("first"),
    );
  }, []);
  if (!mode) return null;
  return mode === "settings" ? (
    <Settings onSetup={() => setMode("again")} />
  ) : (
    <Wizard key={mode} first={mode === "first"} onSettings={() => setMode("settings")} />
  );
}
