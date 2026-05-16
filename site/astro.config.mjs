import { defineConfig } from 'astro/config';

export default defineConfig({
  output: 'static',
  build: { format: 'directory' },
  // Set once you have a domain:
  // site: 'https://your-domain.com',
});
