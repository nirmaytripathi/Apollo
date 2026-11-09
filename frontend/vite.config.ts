// Vite dev server for the Apollo UI. All /api requests (including the SSE
// stream) are proxied to the FastAPI backend, so the frontend never hardcodes
// a backend URL.
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true, // the backend's CORS setting allows exactly this origin
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000' },
    },
  },
})
