import { useState } from "react";
import Onboarding, { type Engine } from "./pages/Onboarding";
import Setup from "./pages/Setup";
import "./App.css";

export default function App() {
  const [engine, setEngine] = useState<Engine | null>(null);
  if (!engine) return <Onboarding onChoose={setEngine} />;
  return <Setup engine={engine} onBack={() => setEngine(null)} />;
}
