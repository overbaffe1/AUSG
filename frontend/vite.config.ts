import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// В контейнере бэкенд живёт по адресу http://api:8000, локально — на 127.0.0.1:8000.
const apiTarget = process.env.COSMO_API_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: Number(process.env.COSMO_WEB_PORT ?? 5017),
    // Горячая перезагрузка через проброшенный том: на Windows и в Docker
    // события файловой системы доходят только опросом.
    watch: { usePolling: true, interval: 300 },
    proxy: { '/api': { target: apiTarget, changeOrigin: true } },
  },
  build: { outDir: 'dist', sourcemap: true },
})
