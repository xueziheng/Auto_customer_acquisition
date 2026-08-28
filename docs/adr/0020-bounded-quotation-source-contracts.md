# ADR 0020：报价来源的有界原件、用途与受限取证契约

日期：2026-08-28。状态：T8A实施中；不表示实际API/worker接线或真实资料验收。

## 决策

新增 `shared.schemas.evidence_read` 与 `shared.evidence_read` 中立契约，将raw metadata、
受限完整bytes、当前用途授权、固定profile正文和版本化定位分开。原文、选区及完整数量事实
不参与普通DTO序列化或repr。资源ID为canonical前缀ULID；员工沿既有 `fact_identity` 的
严格非空、40字符、无首尾空白/控制字符规则兼容历史身份，不以格式代替当前真实权限。

Raw Store的bounded端口与旧get并存：metadata短事务先结束，调用上限与Store上限取交集，
再按实际metadata大小限制transport，最多多读一个哨兵。长度/hash任何不一致拒绝；没有新
transport不回退旧get。infra只映射真实raw原件，不将generated或取得时间当业务证据。

用途为强制discriminated scope。pricing只允许本人上传PDF和成本域操作权限，不叠加CRM范围，
也不授予读他人附件/收件箱或政策写入权限；need_unit精确绑定Need及read/confirm，须真实
Need权限交集、账户和入站消息关系。原件IO前后均重新查当前身份及资料绑定，IO不持业务锁。

`$`仅用于pricing的原件hash核验，不需解析器；片段使用pdf-text-v1或rfc822-plain-v1，
坐标是Python Unicode code point，raw/text/excerpt分别SHA-256。只统一换行，不trim、不做
Unicode规范化、OCR、HTML剥离、金额/单位推断或换算。来源核验本身不证明供应商实报或客户语义。

两种profile固定Linux CPython 3.12.14及pypdf 6.16.2。构造不启动子进程；受信startup显式运行
私有CPU/AS/wall/IPC真实探针后，仅当前实例启用。运行在固定模块的一次性受限worker，使用
有限长度IPC、清洁环境、CPU/AS/wall/并发与队列上限。缺配置、依赖、探针或版本匹配均关闭。
这不是通用代码沙箱；升级需新profile或明确兼容证明及ADR，不能悄悄改变旧定位。

独立 `quotation.evidence.read` LOW/FREE插件只过tenant/permission，并用HMAC绑定请求全部
字段和授权原件。qev槽只在同task成功审计后一次领取；失败只保留固定code，不能将原文
写入ledger/日志或重放。不修改Gateway核心，不将凭证交给上层。

所有资源值必填正整数排bool，无生产默认。本任务仅受控原件/真实PG/Gateway/Linux下层链；
真实API/worker装配、NeedUnitAuthorizer、HTTP和真实商业资料核验由后续T8B另行完成。

## 后果与验证

旧Raw/Generated幂等、写入补偿、T3A单位确认/历史receipt均保持。历史单位只重验metadata
与当前访问权，数量变化不恢复失效单位。单位验证仅为整数与逐字单位相邻的必要词法条件，
语义确认仍属员工责任。所有来源错误为固定中文不可重试code；临时类在Gateway仍正确分类。

验收分别记录有界Store/S3、真实Linux资源、真实PG/Gateway受控原件链；不互相替代。镜像固定
官方index digest和arm64目标，构建白名单输入；纯解析网络none，全链仅专用internal测试PG网络。
