import { svelte } from '@sveltejs/vite-plugin-svelte';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vitest/config';

// The panel is served under a random secret path, so every URL must be relative.
// During development `ICTL_DEV_TARGET` points at a running server's panel URL,
// e.g. http://127.0.0.1:8730/0123456789abcdef
const target = process.env.ICTL_DEV_TARGET;

export default defineConfig({
  base: './',
  plugins: [tailwindcss(), svelte()],
  resolve: { alias: { $lib: '/src/lib', $ui: '/src/ui' } },
  build: {
    outDir: process.env.ICTL_WEB_OUT || '../server/web',
    emptyOutDir: true,
    target: 'es2022',
    cssCodeSplit: true,
    chunkSizeWarningLimit: 400,
  },
  server: target
    ? {
        proxy: {
          '/api': { target: new URL(target).origin, rewrite: (p) => new URL(target).pathname.replace(/\/$/, '') + p },
        },
      }
    : undefined,
  test: { environment: 'node', include: ['src/**/*.test.ts'] },
});
