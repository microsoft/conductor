import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'path';

// Dev-only escape hatch (issue #397): the dashboard's OriginHostGuard
// requires a matching Host header on every request, including the /ws
// handshake. The /api proxy already sets changeOrigin: true; /ws needs it
// too, or the handshake arrives with Host: localhost:5173 and fails the
// guard's host check.
export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    {
      // Injects the dev-server-side CONDUCTOR_GATE_TOKEN into the dev
      // page, mirroring the production server's GET / token injection
      // (web/server.py::index) so the dashboard authenticates identically
      // in dev. Nothing is disabled by this -- every request must still
      // carry the token and pass the origin/host check. Scoped to `serve`
      // (the dev server) only -- `vite build`'s static/index.html is
      // rewritten by the production server on every request instead, so
      // baking an (empty, at build time) token into the committed file
      // would be both wrong and redundant.
      name: 'conductor-inject-dev-token',
      apply: 'serve',
      transformIndexHtml(html: string) {
        const token = process.env.CONDUCTOR_GATE_TOKEN ?? '';
        return html.replace(
          '</head>',
          `<script>window.__CONDUCTOR_TOKEN__=${JSON.stringify(token)};</script></head>`,
        );
      },
    },
  ],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: '../static',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:8000',
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
