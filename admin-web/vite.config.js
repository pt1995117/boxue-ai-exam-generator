import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
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
    port: 8522,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8600',
        changeOrigin: true,
      },
    },
  },
});
