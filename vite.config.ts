import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Minimal Vite config. Everything runs in-browser; no backend.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, open: true },
});
