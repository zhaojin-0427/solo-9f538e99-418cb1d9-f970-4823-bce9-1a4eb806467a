# 样书补印 API

印厂用手头的单面印张和少量空白纸配齐一册样书。本服务提供开批、备用搭配、领纸、补印指令管理，指令和搭配存 SQLite，可按批查询、重启复用。

## 启动

```bash
docker compose up --build
```

服务起在 `http://localhost:8000`，在 **`http://localhost:8000/docs`**（Swagger UI）里即可开批、领纸。SQLite 数据落在名为 `reprint-data` 的卷里（容器内 `/data/reprint.db`，可用环境变量 `REPRINT_DB` 改路径）。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 跑测试
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/batches` | 开批：至多 24 帖（正、反面版号）+ 至多 48 张纸（纸号、已印版号；空白纸留空）。全册配得齐才开批，否则 409 且不落库；同时保存一套每帖一张合用纸的备用搭配 |
| GET | `/batches/{batch_id}` | 批次详情：实际领纸关系、当前备用搭配 `assignment_plan`、每张纸配给/领给哪帖、是否已配齐 |
| GET | `/batches/{batch_id}/assignments` | 查询当前备用搭配；`/plan` 是同义路径 |
| POST | `/batches/{batch_id}/claims` | 领纸：报帖号、纸号，发补印指令（纸号、待印面、版号），并在同一事务内更新备用搭配 |
| GET | `/batches/{batch_id}/instructions` | 按批查询全部补印指令 |

### 开批示例

```json
POST /batches
{
  "signatures": [
    {"signature_no": "S01", "front_plate": "A-01", "back_plate": "A-02"},
    {"signature_no": "S02", "front_plate": "B-01", "back_plate": "B-02"}
  ],
  "sheets": [
    {"sheet_no": "P001", "printed_plate": "A-01"},
    {"sheet_no": "P002", "printed_plate": ""}
  ]
}
```

### 领纸与备用搭配规则

- 开批时用二分图增广路求一套覆盖全部书帖的搭配，存入 `assignments` 表；之后连续发纸优先复用这套搭配。
- 已印版号对上哪一面就补印另一面（上例 `P001` 已印 `A-01`，发给 `S01` 即补印反面 `A-02`）；空白纸两面都印。
- 若工人领的是**当前配给本帖的纸**：发指令，删除本帖的备用搭配，其他帖—纸搭配原样保留，不再做余纸匹配。
- 若领的是一张**合用、尚未领用、也未配给任何余帖的纸**：发指令并释放本帖原备用纸，其他搭配原样保留，也不做余纸匹配。
- 若领走**当前配给别帖的合用纸**：先检查本帖和该纸退出后，其余未领帖与未领纸是否仍有完美搭配。配不齐则 409，不发单、不改搭配；配得齐则发指令，并用带权二分图匹配选择“换纸的未领帖数”最少的新搭配。
- 一帖只发一张、一张纸只发给一帖；同帖换纸或同纸重发给另一帖返回 409。同帖同纸重试返回原指令（`replayed: true`）。
- 指令插入和搭配增删在同一个 `BEGIN IMMEDIATE` 事务中提交；拒绝、约束错误或保存失败都会回滚，保持调用前状态。
- 同批并发领纸由 SQLite 写锁串行化：后提交的请求能看到先提交的领纸/搭配结果；同帖同纸并发重试只产生一张原单。
- 搭配保存在 SQLite 卷中，服务重启后直接复用。升级前创建的旧批没有搭配记录：该批第一次领新单时，会按当时的余帖和未领纸补建；若该次请求被拒绝，补建也一并回滚。

## 实现要点

- **可配性检查**：书帖—纸张构成二分图（合用即有边），用增广路法求最大匹配；开批时要求匹配覆盖全部书帖，跨帖领纸时要求剩余书帖仍可全覆盖。
- **最少换帖重配**：`app/core.py` 的 `minimum_change_plan()` 用小规模最小费用流求完美匹配；保留旧搭配的边费用为 0，换纸边费用为 1，因此得到换纸未领帖数最少的方案。相同换纸数下用纸号顺序费用保证结果稳定。
- **持久化搭配**：`assignments(batch_id, signature_no, sheet_no)` 上有 `UNIQUE(batch_id, signature_no)`、`UNIQUE(batch_id, sheet_no)`；已领帖/纸会从当前搭配删除。另提供 `plan_assignments` 只读视图名称方便旧脚本查询。
- **并发控制**：每请求一个 SQLite 连接，领纸在 `BEGIN IMMEDIATE` 事务内完成“查重 → 判定/重配 → 指令落库 → 搭配更新”，SQLite 写锁把同批并发请求串行化，后到者看到已落库状态后按规则处理；`instructions` 表上的两条唯一约束兜底。失败一律回滚，不占纸、不改搭配。
- **幂等**：同帖同纸的重复请求直接返回库里的原指令。
- **旧库迁移**：启动时 `CREATE TABLE/ VIEW IF NOT EXISTS`，沿用原 SQLite 卷；旧批缺少搭配行时在第一次成功领新单的事务中补建并保存余帖搭配。

## 目录

```
app/
  main.py      # FastAPI 路由、备用搭配复用与领纸事务
  core.py      # 合用判定 + 二分图匹配 + 最少换纸重配
  db.py        # SQLite 连接与建表/兼容视图
  schemas.py   # 请求/响应模型（24 帖 / 48 纸上限在此校验）
tests/test_api.py
tests/test_assignments.py
Dockerfile / docker-compose.yml
```
