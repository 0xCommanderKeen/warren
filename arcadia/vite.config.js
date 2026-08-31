import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/burrow": {
        target: process.env.BURROW_URL || "http://127.0.0.1:8737",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/burrow/, ""),
      },
      "/state": {
        target: process.env.BURROW_URL || "http://127.0.0.1:8737",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
  },
});
