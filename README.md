# 抄表簿

骨架：抄表读数装载 → 估抄/实抄对齐 → 差额处置 → 账期封印。

- 前端 http://127.0.0.1:6010（勿用 6000，浏览器会拦）
- 接口 http://127.0.0.1:11000

## 时钟口径（全局唯一，不得分叉）

实抄同时有 `loaded_at`（服务端装载钟）与 `occurred_at`（客户端发生时刻）。
系统全局只贯彻**装载钟 `loaded_at` 截止**一种架构：

- 对齐 `POST /api/align` 必带 `as_of`，闭包只纳入 `loaded_at <= as_of` 的正式估抄/实抄；
  未来时刻、非法格式、缺失的 `as_of` 一律 400；请求 `clock=occurred_at` 一律 400。
- `occurred_at` 仅作审计，出现在闭包快照的审计列，不参与任何 WHERE 截止。
- 每次对齐把闭包逐行写入 `reconcile_snapshots`，批次固化 `clock/as_of/fingerprint`。
- 封印 `POST /api/seal` 只认批次固化的同一只钟、同一个 `as_of`，并用该 `as_of`
  重算闭包比对指纹：不一致（换钟/换时刻/闭包漂移）即 409；封印期快照可经
  `GET /api/snapshots?batch_id=` 回溯。
- 装载、对齐、封印全部走 `BEGIN IMMEDIATE` 串行化事务，并发下只可能看到
  完整事务前缀，不存在半截混钟。
- 口径回显：`GET /api/clock` 与 `/api/overview` 的 `clock` 字段恒为 `loaded_at`。

## 重跑对齐口径（命运架构，全局唯一）

重跑对齐存在两种可选架构，本系统全局只贯彻**命运架构——以已决命运与痕迹为准**：

- 重跑先在同一 `IMMEDIATE` 事务内把旧开放批整体交接：批次置 `superseded`，
  其名下差额全部打 `superseded_at` 痕迹（行不删，可审计），再写新批；
  一个账期至多一个 `open` 现行批，同键禁止两条现行差额（部分唯一索引
  `ux_variances_live` 在数据库层硬保证）。
- **已处置键不被重跑吞掉**：`fate != pending` 的键，新批不再生成其差额，
  处置命运随旧批留痕；新批只能收仍未决的现行键。
- 快照键集合 = 闭包键集合（每批自洽）；同 `as_of` 重跑时，现行未决键 ∪ 已决留痕键
  恰好等于快照键集合；闭包缩窄（更早 `as_of`）而掉出快照的已决键仍以痕迹保留可审计。
- 总览未决只数现行 `pending`（`superseded_at IS NULL`），恒等于现行批未决数；
  `superseded` 批不再驱动任何未决计数，禁止多批加总。`GET /api/rollup` 同时
  给出现行口径 `live_pending` 与仅供审计的 `trace_pending`。
- 对齐与处置抢跑、或提交前任一步失败：整笔事务回滚，不留半截批、不留双现行；
  已被取代的痕迹行禁止再改命运（409）。
- 另一套"以最新对齐批键集合为准、已决键进隔离重开"的架构明确不采用，代码中
  不得出现第二套口径。

## 启动

```powershell
cd backend
C:\Users\18435\AppData\Roaming\uv\python\cpython-3.11.15-windows-x86_64-none\python.exe -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 11000 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 6010
```
