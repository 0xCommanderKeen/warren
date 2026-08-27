import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App.jsx";
import { createStewardClient } from "./steward/StewardClient.js";
import "./styles.css";

const stewardBaseUrl = new URLSearchParams(window.location.search).get("steward") ||
  import.meta.env.VITE_STEWARD_URL || "";
const stewardClient = createStewardClient({ baseUrl: stewardBaseUrl });

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App stewardClient={stewardClient} />
  </StrictMode>,
);
