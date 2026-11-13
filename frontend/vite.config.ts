import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 前端默认用同源 /api（生产 nginx 反代）；本地 dev/preview 由这里代理到 FastAPI 8010。
const apiProxy = {
  '/api': {
    target: 'http://127.0.0.1:8010',
    changeOrigin: true,
  },
};

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5180,
    strictPort: true,
    proxy: apiProxy,
  },
  preview: {
    host: '0.0.0.0',
    port: 5180,
    strictPort: true,
    proxy: apiProxy,
  },
});
