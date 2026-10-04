# 开放日多资源排班

大型开放日同时安排讲解员、讲师、场地与学校团体。本服务维护人员资格、场地容量、
可用时段、跨展厅移动缓冲与场次依赖，在排定前生成候选方案；确认时在同一数据库
事务内原子锁定全部资源，并对部分资源拒绝、学校人数变化、并发确认、取消释放与
重启回收提供一致性保证。冲突时返回结构化阻塞原因与可解释替代方案。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/scheduling/`：排班服务。
  - `models.py`：人员/场地/团体/场次/候选/排期单模型与状态常量。
  - `catalog.py`：资格、容量、可用时段、移动缓冲主数据。
  - `occupancy.py`：人员（含缓冲）与场地占用冲突检测。
  - `planner.py`：候选生成、场次依赖排序、阻塞诊断与替代方案。
  - `storage.py`：SQLite 持久化（`BEGIN IMMEDIATE` 写事务）。
  - `service.py`：暂占（hold）、确认（confirm）、取消（cancel）、
    人数变化联动、过期回收。
  - `api.py` / `__main__.py`：标准库 HTTP 服务。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域服务（17 例）与 HTTP 端到端（3 例）测试。

## 运行

```bash
python3 -m pip install -e .            # 可选；也可直接 PYTHONPATH=src
PYTHONPATH=src python3 -m scheduling --db scheduling.db --seed --port 8080
```

时间在服务内部以 epoch 分钟存储，HTTP 边界接受/返回 ISO-8601（UTC，如
`2026-10-10T09:00Z`）。

## 排期单状态

| 状态 | 含义 |
|---|---|
| `held` | 待确认暂占，带 TTL（默认 10 分钟） |
| `confirmed` | 已排定，全部资源原子锁定 |
| `cancelled` | 主动取消，资源立即释放 |
| `expired` | TTL 过期、人数变化联动、或确认复检失败而释放 |
| `superseded` | 并发确认中被获胜排期原子作废 |

## 关键语义

- **资格/容量/时段**：候选必须满足角色与资格、团体人数不超场地容量、
  人员与场地均在可用时段内。
- **移动缓冲**：同一讲解员/讲师相邻场次在不同展厅时，按人员 `travel`
  配置（无向，缺省 30 分钟）拉开间隔；同厅连续无缓冲。
- **场次依赖**：`after` 声明本场必须晚于另一场次结束 + 间隔；依赖环在
  排定前报错。
- **软竞争 vs 硬冲突**：`/plan` 以已确认占用为硬约束，以待确认暂占为软竞争，
  软竞争候选带 `contended: true` 并排在无竞争候选之后（始终保留最优竞争候选
  供知情抢占）；`/reservations` 允许两个团体暂占同一资源。
- **原子确认**：确认时在单事务内复检资格/容量/时段/已确认占用；通过则锁定
  本单并把冲突暂占标记为 `superseded`，未冲突暂占不受影响。复检失败则本暂占
  标记为 `expired(resources_refused)`，未锁定任何资源，响应附带最新替代方案。
- **并发**：服务层互斥锁 + SQLite 写事务串行化确认；支持 `expected_version`
  乐观锁。N 个并发确认恰好一个成功，其余得到 409 + 可解释诊断。
- **人数变化**：会使已排定场次超容的变更被拒绝并列出受影响场次；使待确认
  暂占超容的变更原子失效该暂占。
- **过期回收**：每次操作前惰性回收；服务启动（重启）时统一回收全部过期暂占，
  也可 `POST /reap-expired` 主动触发。

## HTTP 接口

| 方法与路径 | 说明 |
|---|---|
| `POST /admin/staff` `/venues` `/groups` | 维护人员/场地/团体 |
| `POST /groups/{id}/headcount` | 学校人数变化 |
| `POST /plan` | 生成候选方案 + blockers + suggestions |
| `POST /reservations` | 选定候选创建暂占 |
| `POST /reservations/{id}/confirm` | 原子确认（409 时返回拒绝明细与替代方案） |
| `POST /reservations/{id}/cancel` | 取消并释放 |
| `GET /reservations[/{id}]` | 查询 |
| `POST /reap-expired` | 主动回收过期暂占 |

`/plan` 响应中的 `suggestions[].kind`：`WAIT_FOR_HOLDS`（等待竞争暂占释放，
附最早过期时间）、`EXTEND_WINDOW`（延后时间窗后的可行方案）、`RELAX_VENUE`
（放弃指定展厅后的调配方案）、`SHRINK_GROUP`（人数超过所有场地容量）。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
