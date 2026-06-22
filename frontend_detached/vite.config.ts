import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig(() => ({
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: false,
    proxy: {
      "/summary": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/sensor_categories": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/countries": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/regions": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/country_region_data": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/get_stations": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/bbox_data": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/station_data": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/station_readings": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },

  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
}));
