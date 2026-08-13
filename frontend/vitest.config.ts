import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { resolve } from 'path'

export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.{test,spec}.{js,ts,tsx}'],
    exclude: ['node_modules', 'dist'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      // Production code lives at the top level, NOT under src/ — which holds only index.css and
      // assets/. This said `src/**` and so measured nothing at all, which meant the thresholds
      // below passed vacuously on an empty set and reported success for zero coverage.
      include: ['services/**/*.{ts,tsx}', 'components/**/*.{ts,tsx}', 'hooks/**/*.{ts,tsx}', '*.tsx'],
      exclude: [
        'src/main.tsx',
        'src/vite-env.d.ts',
        '**/*.d.ts'
      ],
      // A FLOOR set to roughly what the suite actually achieves today, not a target. It exists to
      // stop coverage falling further; raise it as real tests land. The previous 60s were not a
      // stricter version of this — they were measuring an empty file set. Note CI runs `test:run`,
      // so these gate `npm run test:ci` only.
      thresholds: {
        statements: 18,
        branches: 70,
        functions: 33,
        lines: 18
      }
    }
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, './src')
    }
  }
})
