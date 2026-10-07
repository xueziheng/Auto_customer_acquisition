import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { readFile } from "node:fs/promises";
export default defineConfig({
  plugins: [vue(), {
    name: "controlled-current-health",
    configureServer(server) {
      server.middlewares.use("/__controlled/status", async (request, response) => {
        response.setHeader("Content-Type", "application/json");
        response.setHeader("Cache-Control", "no-store");
        try {
          if (request.method !== "GET" || request.url !== "/") throw new Error();
          const state = JSON.parse(await readFile(process.env.CONTROLLED_STATUS_PATH ?? "", "utf8"));
          const age = Date.now() / 1000 - state.updated_at;
          const fresh = age >= 0 && age <= 4;
          response.end(JSON.stringify({ status: fresh ? state.status : "unknown", reason: fresh ? state.reason : "supervisor_unavailable" }));
        } catch { response.statusCode = 503; response.end('{"status":"unknown","reason":"supervisor_unavailable"}'); }
      });
    },
  }],
  envDir: false,
  server: { host: "127.0.0.1", strictPort: true, allowedHosts: ["127.0.0.1"] },
});
