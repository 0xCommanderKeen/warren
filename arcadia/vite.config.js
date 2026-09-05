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
    },
  },
  test: {
    exclude: [...configDefaults.exclude, "browser/**"],
    environment: "jsdom",
  },
});
