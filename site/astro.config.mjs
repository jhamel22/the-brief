import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

export default defineConfig({
  site: 'https://the-brief.pages.dev',
  output: 'static',
  build: { format: 'directory' },
  integrations: [sitemap()],
});
