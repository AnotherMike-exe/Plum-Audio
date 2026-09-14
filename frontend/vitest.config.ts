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
      //
      // Lowered on the vitest 3 -> 4 bump. NOT a coverage regression: the same 174 tests cover the
      // same code, and vitest 4's v8 provider counts it differently (branches read 77% under 3 and
      // 16% under 4, on an unchanged suite). These numbers are the new measurement's floor. Do not
      // compare them against a pre-4 report.
      thresholds: {
        statements: 17,
        branches: 16,
        functions: 15,
        lines: 17
      }
    }
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, './src')
    }
  }
})
