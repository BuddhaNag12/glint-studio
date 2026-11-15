import { useState } from "react";
import Onboarding, { type Engine } from "./pages/Onboarding";
import "./App.css";

export default function App() {
  const [engine, setEngine] = useState<Engine | null>(null);

  if (!engine) return <Onboarding onChoose={setEngine} />;
  // shortcut: the install step (runtime.rs) isn't built yet; this screen is where it goes.
  return (
    <main className="page">
      <h1>{engine === "local" ? "On this computer" : "With a cloud provider"}</h1>
      <p className="lead">Setup for this choice comes in the next build.</p>
      <button onClick={() => setEngine(null)}>Back</button>
    </main>
  );
}
