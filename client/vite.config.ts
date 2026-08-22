import { defineConfig } from "vite";

const stellarium = "http://127.0.0.1:8090";

export default defineConfig({
  server: {
    port: 3001,
    proxy: {
      "/api": {
        target: stellarium,
        changeOrigin: false,
      },
    },
  },
  preview: {
    port: 3001,
    proxy: {
      "/api": {
        target: stellarium,
        changeOrigin: false,
      },
    },
  },
});
