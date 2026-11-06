import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        // Recharts is most of the weight and changes far less often than the
        // app, so it is worth its own long-lived chunk.
        manualChunks: {
          charts: ["recharts"],
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query", "zustand"],
        },
      },
    },
  },
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    host: true,
    port: 5173,
    // Dev-only proxy so the browser talks to one origin and cookies just work.
    proxy: {
      "/api": {
        target: process.env.VITE_API_URL ?? "http://localhost:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
