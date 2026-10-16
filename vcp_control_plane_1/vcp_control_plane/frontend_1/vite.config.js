import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Docker bind mounts on Windows don't deliver file-change events, so poll instead
    watch: { usePolling: true, interval: 500 },
    proxy: {
      '/api': {
        target: process.env.VITE_API_BASE_URL
          ? process.env.VITE_API_BASE_URL.replace('/api/v1', '')
          : 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
