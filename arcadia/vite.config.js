import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";
import { configDefaults } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/chronicle": {
        target: process.env.CHRONICLE_URL || "http://127.0.0.1:8737",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/chronicle/, ""),
      },
      "/state": {
        target: process.env.CHRONICLE_URL || "http://127.0.0.1:8737",
        changeOrigin: true,
      },
      // Keep local operator actions on the same origin as the village, just
      // like deployed nginx. Credentials are still supplied by the operator.
      ...Object.fromEntries(
        ["residents", "skills", "reload", "jobs", "tasks", "delegate", "approvals",
          "routines", "org", "requests", "secrets"].map(path => [
          `/${path}`,
          { target: process.env.STEWARD_URL || "http://127.0.0.1:8801", changeOrigin: true },
        ]),
      ),
    },
  },
  test: {
    exclude: [...configDefaults.exclude, "browser/**"],
    environment: "jsdom",
  },
});
