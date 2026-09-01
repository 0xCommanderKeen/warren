import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/state": {
        target:
          process.env.CHRONICLE_URL ||
          process.env.BURROW_URL ||
          "http://127.0.0.1:8737",
        changeOrigin: true,
      },
      // Steward's routes, so `pnpm dev` reaches them same-origin exactly as the deployed
      // nginx does. The token is still typed by a human at runtime. This list and the
      // regex in `arcadia/deploy/nginx.conf` are the same route table written twice: a
      // path in one and not the other is a page that works in dev and 404s deployed, or
      // the reverse (warren#242).
      ...Object.fromEntries(
        [
          "/residents",
          "/skills",
          "/reload",
          "/jobs",
          "/tasks",
          "/delegate",
          "/approvals",
          "/routines",
          "/requests",
        ].map(
          (path) => [
            path,
            { target: process.env.STEWARD_URL || "http://127.0.0.1:8801", changeOrigin: true },
          ],
        ),
      ),
    },
  },
  test: {
    // The write surface is a set of forms; a form is not tested by reading it.
    environment: "jsdom",
    globals: false,
    restoreMocks: true,
  },
});
