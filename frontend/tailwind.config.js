/** Plum-Audio GUI — Tailwind config.
 *
 * The ported Snapcast GUI originally pulled Tailwind from cdn.tailwindcss.com. That is a dev-only
 * JIT build AND an internet dependency at page load — untenable for a unit on an isolated AV VLAN,
 * so Tailwind is compiled into the bundle here instead. Scanning is deliberately broad: the app
 * leans on arbitrary values (bg-[var(--bg-primary)]) and template-built class strings.
 */
export default {
    content: ['./index.html', './*.{ts,tsx}', './{components,hooks,services,utils,src}/**/*.{ts,tsx}'],
    theme: { extend: {} },
    plugins: [],
}
