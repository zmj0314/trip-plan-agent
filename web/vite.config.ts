import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000";

// Vite's default host ("localhost") resolves to ::1 only on Windows, so
// http://127.0.0.1:5173 is refused while http://localhost:5173 works -- an
// easy way to conclude "the page is down" when it is actually running.
// Bind IPv4 loopback explicitly; set VITE_HOST=lan to expose on the network.
const host = process.env.VITE_HOST === "lan" ? true : "127.0.0.1";

export default defineConfig({
  plugins: [react()],
  server: {
    host,
    port: 5173,
    strictPort: true,
    proxy: {
      "/sessions": { target: apiTarget, changeOrigin: true },
      "/llm": { target: apiTarget, changeOrigin: true },
      "/healthz": { target: apiTarget, changeOrigin: true }
    }
  }
});
