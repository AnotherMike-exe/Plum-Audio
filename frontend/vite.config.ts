import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
    const env = loadEnv(mode, process.cwd(), '')

    // Config APIs (settings now; integrations/audio/playback later) are served by the Flask host
    // on a unit's :5002. settingsService calls them via a relative /api/* path, so for local
    // `npm run dev` against a real unit we proxy those paths there. In production the container's
    // reverse proxy handles it and this proxy is unused. (The mesh API on :5001 is reached
    // directly via VITE_MESH_API_URL, not proxied.)
    const configApi = env.VITE_SETTINGS_API_URL
    const proxy = configApi
        ? Object.fromEntries(
              ['/api/settings', '/api/integrations', '/api/audio', '/api/playback'].map((path) => [
                  path,
                  { target: configApi, changeOrigin: true },
              ]),
          )
        : undefined

    return {
        plugins: [react()],
        server: {
            host: '0.0.0.0',  // This allows external access
            port: 5173,
            proxy,
        },
        preview: {
            host: '0.0.0.0',
            port: 5173,
        },
        build: {
            outDir: 'dist',
            assetsDir: 'assets',
        },
    }
})
