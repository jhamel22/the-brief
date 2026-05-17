import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  output: 'static',
  site: 'https://the-brief.pages.dev',
  build: { format: 'directory' },
  integrations: [sitemap()],
});
