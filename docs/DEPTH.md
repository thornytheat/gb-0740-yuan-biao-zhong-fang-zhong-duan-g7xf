# 深度登记 源表重放终端

- 规模：中型
- 核心规则：先扩展受影响的分区与跨日依赖，再生成写入计划，验证每个目标指针并演练原样回滚。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 分区登记 | 采集快照与变更并逐源行复核 | `GET /api/inventory/{id}` | `app/modules/m1_inventory/service.py` | `tests/test_inventory.py` |
| M2 依赖求解 | 查拓扑序、日期偏移与缺表 | `GET /api/graph/{id}` | `app/modules/m2_graph/service.py` | `tests/test_graph.py` |
| M3 清单指纹 | 核对预期产物版本与源数 | `GET /api/fingerprint/{id}` | `app/modules/m3_fingerprint/service.py` | `tests/test_fingerprint.py` |
| M4 重放排程 | 按状态检查挂起和依赖变化 | `GET /api/schedule/{id}` | `app/modules/m4_schedule/service.py` | `tests/test_schedule.py` |
| M5 回滚校验 | 核对写入前后与恢复哈希 | `GET /api/rollback/{id}` | `app/modules/m5_rollback/service.py` | `tests/test_rollback.py` |
| M6 行数勾稽 | 守恒比较影响分区、源记录和行数 | `GET /api/reconcile/{id}` | `app/modules/m6_reconcile/service.py` | `tests/test_reconcile.py` |
| M7 版本签发 | 在近期计划中复用并下载清单 | `GET /api/release/{id}` | `app/modules/m7_release/service.py` | `tests/test_release.py` |
| M8 隔离处置 | 登记源表校验异常，签发解除凭证并演练解除 | `GET /api/quarantine/{id}` | `app/modules/m8_quarantine/service.py` | `tests/test_quarantine.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | clean.json 加 BOM | 首字段异常 | UTF-8 BOM 解码 | `app/modules/m1_inventory/reader.py` | `tests/test_inventory.py::test_bom` |
| TRAP-06 | nightly.json 的行数全角千分位 | 按原文解析失败 | 正则后归一 | `app/keys.py` | `tests/test_inventory.py::test_fullwidth_grouping` |
| TRAP-07 | nightly.json 行数 N/A | 当作零 | 隔离无效变更 | `app/modules/m1_inventory/service.py` | `tests/test_inventory.py::test_missing_value_isolated` |
| TRAP-09 | 超大汇总整数由两个大分区产生 | 浏览器舍入 | 安全范围外用字符串 | `app/modules/m6_reconcile/service.py` | `tests/test_reconcile.py::test_aggregated_large_counts_are_exact_strings` |
| TRAP-14 | nightly.json 同分区改两次 | 最后一次覆盖 | 整组隔离 | `app/modules/m1_inventory/service.py` | `tests/test_inventory.py::test_conflict_both_reviewed` |
| TRAP-16 | clean.json 尾部截断 | 保留半表 | 结构完整才计算 | `app/modules/m1_inventory/reader.py` | `tests/test_inventory.py::test_truncated_input_rejected` |
| TRAP-17 | nightly.json 旧字段 count | 字段缺失 | 择一读取旧别名 | `app/modules/m1_inventory/service.py` | `tests/test_inventory.py::test_legacy_count` |
| TRAP-20 | clean.json 添加回边 | 排序丢结点或死循环 | 深度优先检测访问栈 | `app/modules/m2_graph/service.py` | `tests/test_graph.py::test_cycle_path_from_sample` |
| TRAP-23 | nightly.json 截点紧邻滞后一日 | 越界写入 | 分区日显式切界 | `app/modules/m4_schedule/expansion.py` | `tests/test_schedule.py::test_cutoff_included_but_lagged_next_day_excluded` |
| TRAP-26 | 演练前计划引用旧指针 | 覆盖新版本 | 检查所有写入前置值 | `app/modules/m5_rollback/service.py` | `tests/test_rollback.py::test_stale_snapshot_atomic` |
| TRAP-27 | 只放开直接子分支 | 多跳间接受影响的表漏判 | 从异常源分区沿反向依赖 BFS 圈定全边界 | `app/modules/m8_quarantine/service.py` | `tests/test_quarantine.py::test_boundary_covers_multi_hop` |
| TRAP-28 | 解除中途指针已变动 | 半解除状态残留 | 整组恢复冻结前旧版结果再报失败 | `app/modules/m8_quarantine/service.py` | `tests/test_quarantine.py::test_release_restores_pre_freeze_versions_on_failure` |
| TRAP-29 | 原异常证据被误删 | 已解除范围被当作仍可复核 | 凭证只钉证据摘要，回查必须读到原证据 | `app/modules/m8_quarantine/service.py` | `tests/test_quarantine.py::test_released_scope_not_auditable_after_evidence_deleted` |
| TRAP-30 | 同一故障重复上报 | 每次重试都新增凭证 | 凭证编号由计划与故障内容哈希决定 | `app/modules/m8_quarantine/service.py` | `tests/test_quarantine.py::test_same_fault_retry_issues_no_second_credential` |

## 已知边界

清单约定按日分区、拼接型节点、离线镜像演练。读源快照与分区日的证据随签发清单输出。

隔离处置以签发计划为上下文：凭证从计划的依赖图与现行指针圈定冻结边界，解除与回查均为样例快照上的演练，实际仓库的调度与执行另由值班系统负责。凭证不内嵌证据原文，只钉证据摘要；原异常证据删除后，已解除范围即无法复核。
