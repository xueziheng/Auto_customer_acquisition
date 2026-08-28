# evidence_text：纯离线受限来源解析

遵守根硬边界1/4/5/8/9和connectors规则。仅解析已授权取得的受限bytes，不持凭证，
不查数据库、不判断单位/金额/权限，不访问网络或OCR，不跨apps导入旧parser。

pdf-text-v1及rfc822-plain-v1仅Linux CPython3.12.14+pypdf6.16.2可用。构造零spawn，
必须受信startup显式probe，本实例私有状态才启用。所有限额显式正int排bool，无生产默认。
worker使用固定安装模块、清洁环境、有界长度帧，一次性进程；读取内容和导入入解析库前设CPU/AS/core。
原文不进入argv、repr、日志、异常或普通DTO序列化；stderr丢弃。取消/timeout回收后才结束。
probe仅私有固定模块，不在parse协议/manifest提供mode/fault/program字段，不能从HTTP传票据启用。
版本升级需新profile或兼容证明及ADR。此能力不是通用沙箱，资料ACL与Gateway审计仍不可省略。
