# infra/controlled：本机受控基础设施

遵守根九条边界和infra规则。只管理本owner资源、严格配置与外部Provider响应；
禁止import apps/tests，禁止写业务种子或审批编排。临时配置0600、目录0700，
凭证不得出现在repr、异常、日志。清理前核验owner标签及容器ID或PID出生时间。
