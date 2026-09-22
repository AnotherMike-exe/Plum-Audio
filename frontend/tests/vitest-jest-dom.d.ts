/**
 * jest-dom matcher types for Vitest 5.
 *
 * @testing-library/jest-dom ships a Vitest augmentation of its own, but as of 7.0.1 it still
 * declares `interface Assertion<T = any>` — the single-parameter shape Vitest 4 had. Vitest 5's
 * is `Assertion<R extends void | Promise<void>, T>`, and declaration merging requires identical
 * type parameter lists, so that augmentation silently fails to apply. The symptom is every
 * `expect(el).toBeInTheDocument()` failing tsc with TS2339 while passing at runtime — which is
 * why `npm run test:run` stayed green through the Vitest 5 bump and only `tsc --noEmit` caught it.
 *
 * `Matchers<R, T>` is the empty interface Vitest 5 exposes for exactly this purpose, so map
 * jest-dom's matchers onto that instead. Both parameters have to be restated to merge, even
 * though only R is used. Delete this file once jest-dom ships Vitest 5 types.
 *
 * Runtime is unaffected either way: tests/setup.ts registers the matchers with expect.extend().
 */
import type { TestingLibraryMatchers } from '@testing-library/jest-dom/matchers'

declare module 'vitest' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface Matchers<R extends void | Promise<void> = void | Promise<void>, T = unknown>
    extends TestingLibraryMatchers<any, R> {}
}
