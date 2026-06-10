import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The frontend talks to the FastAPI backend at http://localhost:8000 directly
// (the backend enables CORS for http://localhost:5173). Override the base URL
// with VITE_API_BASE if the API runs elsewhere.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
  },
});
