import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
// 开发模式：/api 代理到后端 8000 端口（免 CORS）
export default defineConfig({
    plugins: [react()],
    resolve: {
        alias: {
            '@': fileURLToPath(new URL('./src', import.meta.url)),
        },
    },
    server: {
        port: 5173,
        proxy: {
            '/api': {
                target: 'http://localhost:8000',
                changeOrigin: true,
            },
        },
    },
    build: {
        chunkSizeWarningLimit: 1500,
        rollupOptions: {
            output: {
                manualChunks: {
                    antd: ['antd', '@ant-design/icons'],
                    react: ['react', 'react-dom', 'react-router-dom'],
                    markdown: ['react-markdown', 'remark-gfm'],
                },
            },
        },
    },
});
