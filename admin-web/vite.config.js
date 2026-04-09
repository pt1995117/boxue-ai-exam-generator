import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

const sharedHost = 'ai-exam.ttb.test.ke.com';
const disableHmr = process.env.DISABLE_HMR === '1' || process.env.VITE_DISABLE_HMR === '1';
const defaultCacheRoot = path.join(process.cwd(), '..', '.local', 'cache');
const cacheRoot = process.env.BOXUE_CACHE_DIR || defaultCacheRoot;

export default defineConfig({
  plugins: [react()],
  cacheDir: path.join(cacheRoot, 'admin-web', 'vite'),
  build: {
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          antd: ['antd', '@ant-design/icons', '@ant-design/pro-components'],
          vendor: ['axios'],
        },
      },
    },
  },
  server: {
    host: '0.0.0.0',
    port: 8522,
    allowedHosts: [sharedHost],
    // Shared remote access through a gateway is more stable without Vite HMR.
    ...(disableHmr ? { hmr: false } : {}),
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8600',
        changeOrigin: true,
      },
    },
  },
});
