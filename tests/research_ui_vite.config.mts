// 仅供受控本地 UI 预览；同 origin 代理仍保留真实 API 身份头和确认门。
import base from "../apps/web/vite.config.ts";

export default {
  ...base,
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8184",
        rewrite: (path: string) => path.replace(/^\/api/, ""),
      },
    },
  },
};
