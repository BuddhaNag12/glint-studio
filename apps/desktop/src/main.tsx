import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { applyAppearance } from "./dashboard/settings/Appearance";

applyAppearance(); // the theme picked in Settings, before the first render

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
