# 开放日多资源排班

本项目维护开放日多资源排班的领域约定、角色边界与样例数据，并交付一个**零外部依赖的 Python 服务端**，供后端服务、接口和自动化验证统一使用。覆盖讲解员、讲师、场地（展厅）、学校团体四类资源，落实组合资源约束、时段缓冲冲突、原子确认占用、过期暂占回收四项不变量。

## 领域模型与不变量

| 资源 | 关键属性 | 占用语义 |
| --- | --- | --- |
| 讲解员 `guide` / 讲师 `lecturer` | 资格标签、可用时段、移动缓冲（分钟）、容量（可并行场次） | 同一时段一场；相邻两场间隔须 ≥ 各自缓冲 |
| 场地 `venue` | 人数容量、可用时段、移动缓冲 | 同一时刻**单场独占**；学校人数不得超容量 |
| 学校团体 `group` | 可变人数、联系人、转场缓冲 | 同一时段一场；人数变化在确认时重新校验 |

- **组合资源约束**：场次按「角色 + 资格」组合选人（如需要一名具备 `lec:space` 资格的讲师 + 一个装得下团体的展厅），引擎自动在合格资源中择优；也可 `resource_id` 指定。
- **场次依赖**：`depends_on` 声明前置场次（可在同一候选批次内，也可指向已确认的外部场次），前置场次未排定或结束晚于本场开始即冲突；依赖成环直接报错。
- **原子确认占用**：`plan` 只产出候选并写入带 TTL 的暂占；`confirm` 在单个 SQLite 事务（`BEGIN IMMEDIATE`）内重新校验全部约束后把暂占转正，任一资源被抢走则整体回滚，绝不留半锁。
- **过期暂占回收**：候选有 TTL，每次操作前自动回收；服务重启时构造函数立即回收上次进程遗留的过期暂占。
- **可解释冲突**：冲突响应逐条给出资源、类型（`qualification` / `capacity` / `double_booking` / `buffer` / `availability` / `dependency` / `inactive`）、中文原因、冲突场次与时间区间，并附**同角色备选资源**与**前后可平移的时间段**。

## 两阶段协议

```
统筹员                服务                      其它统筹员
  │  POST /plans       │                          │
  │ ─────────────────► │ 快照校验 → 候选+暂占(TTL) │
  │  候选方案/冲突+替代  │                          │
  │                    │ ◄──────── 规划/确认占用 ──│
  │  POST /plans/{id}/confirm                     │
  │ ─────────────────► │ 单事务全量复检：           │
  │  confirmed(原子锁)  │  通过→暂占转正 / 否则全回滚 │
  │  或 409+替代方案    │                          │
  │  POST /plans/{id}/cancel（pending 释放暂占 / confirmed 释放排期）
```

人数变化的两种一致性策略：确认时默认按当前人数复检（可行则带新人数确认并回报 `group_size_changes`）；若客户端持旧快照，可传 `expected_group_sizes`，不一致立即得到 `409 stale_candidate_version`。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/scheduling/`：排班服务端
  - `models.py` 领域模型（epoch 分钟整数时间）；`clock.py` 可注入时钟；
  - `candidates.py` 候选引擎（资格/容量/缓冲/依赖求解 + 替代方案）；
  - `store.py` SQLite 持久层（资源/团体/候选/暂占/排期/拒绝时段）；
  - `service.py` 应用服务（两阶段协议、原子确认、回收、线程串行化）；
  - `api.py` 标准库 HTTP 接口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约与服务回归测试（36 个用例，含并发压力、HTTP 端到端与重启回收）。

## HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `POST /resources` | 登记/更新讲解员、讲师、场地（版本自增） |
| `GET /resources`、`GET /resources/{id}` | 查询 |
| `POST /resources/{id}/reject` | 部分资源拒绝某时段（`{start,end,reason}`） |
| `POST /resources/{id}/active` | 停用/启用资源 |
| `POST /groups`、`GET /groups` | 学校团体 |
| `POST /groups/{id}/size` | 学校人数变化 |
| `POST /plans` | 阶段一：生成候选方案（不可行返回 409 + conflicts/alternatives） |
| `POST /plans/{id}/confirm` | 阶段二：原子锁定全部资源 |
| `POST /plans/{id}/cancel` | 取消并释放（暂占或已确认占用） |
| `GET /plans/{id}`、`GET /schedule`、`GET /stats` | 查询 |
| `POST /reap` | 手动触发过期回收 |

`POST /plans` 请求示例：

```json
{
  "ttl_seconds": 600,
  "sessions": [
    {"session_id": "s1", "title": "开场讲座",
     "start": "2026-10-01T09:00Z", "end": "2026-10-01T10:00Z",
     "group_id": "sch1",
     "roles": [
       {"kind": "guide", "qualifications": ["q:base"]},
       {"kind": "lecturer", "qualifications": ["lec:space"]},
       {"kind": "venue"}
     ]},
    {"session_id": "s2", "title": "展厅导览",
     "start": "2026-10-01T10:30Z", "end": "2026-10-01T11:30Z",
     "group_id": "sch1", "depends_on": ["s1"],
     "roles": [{"kind": "guide", "qualifications": ["q:vip"]},
               {"kind": "venue"}]}
  ]
}
```

## 运行

```bash
# 启动服务（默认 data/scheduler.db，零依赖，Python 3.11+）
PYTHONPATH=src python3 -m scheduling.api --db data/scheduler.db --port 8080
```

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
