import { defineConfig } from "vite";
import dyadComponentTagger from "@dyad-sh/react-vite-component-tagger";
import react from "@vitejs/plugin-react-swc";
import path from "path";

export default defineConfig(() => ({
  server: {
    host: "::",
    port: 8080,
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

  plugins: [dyadComponentTagger(), react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
}));
