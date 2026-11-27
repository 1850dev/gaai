import adapter from '@sveltejs/adapter-node';
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  worker: { format: 'es' },
  plugins: [sveltekit({ adapter: adapter({ out: 'build' }) })],
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node'
  }
});
