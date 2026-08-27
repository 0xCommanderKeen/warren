import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
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
