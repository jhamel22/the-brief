import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://the-brief.pages.dev',
  output: 'static',
  build: { format: 'directory' },
});
