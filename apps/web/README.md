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

所有 API 身份由 `src/api/client.ts` 的统一中间件注入，页面组件不得自行设置
`X-Tenant-Id` 或 `X-Employee-Id`。Vite 开发模式只有在 `VITE_TENANT_ID` 与
`VITE_EMPLOYEE_ID` 同时固定配置时才允许使用断言身份；生产模式必须先通过认证
启动流程调用 `configureAuthenticatedIdentity`，否则请求会在发出前失败关闭。
