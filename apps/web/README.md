# TradeOS Web

Web 前端使用 Vue 3、TypeScript、Vite 与 Ant Design Vue。API 类型由后端零参数 `create_app()` 工厂导出的 OpenAPI 契约生成，不能手写重复 DTO。

```bash
npm ci
npm run gen:api
npm run typecheck
npm run lint
npm run test
npm run build
```

`npm run gen:api` 只把临时 OpenAPI JSON 通过标准输出传给生成器；仓库中唯一提交的生成物是 `src/api/api.d.ts`。
