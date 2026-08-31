import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { LiveApp } from "./App.jsx";
import { createStewardClient, stewardBaseFromLocation } from "./steward/StewardClient.js";
import "./styles.css";

const stewardClient = createStewardClient({ baseUrl: stewardBaseFromLocation() });

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <LiveApp stewardClient={stewardClient} />
  </StrictMode>,
);
