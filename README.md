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
