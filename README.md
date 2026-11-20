# 数据分析实验与数据应用服务平台

面向指标口径管理、分析实验设计、OLAP查询、数据看板构建与数据API发布的数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 指标 | 指标编号、指标名称、计算公式 |
| 维度管理 | `dimension` | 分析维度 | 维度编号、维度名称、维度类型 |
| 数据集管理 | `dataset` | 分析数据集 | 数据集编号、数据集名称、来源类型 |
| 分析实验 | `experiment` | 实验方案 | 方案编号、实验名称、分析数据集 |
| OLAP查询 | `query_builder` | 查询模板 | 模板编号、模板名称、查询语句 |
| 数据看板 | `dashboard` | 看板配置 | 看板编号、看板名称、包含图表 |
| 图表配置 | `chart` | 图表 | 图表编号、图表名称、图表类型 |
| 数据API发布 | `data_service` | 数据服务 | 服务编号、服务名称、输出数据集 |
| 智能告警 | `alert` | 告警规则 | 规则编号、告警名称、监控指标 |
| 分析用户组 | `user_group` | 用户组 | 组编号、组名称、组描述 |
| 数据快照 | `snapshot` | 快照记录 | 快照编号、数据集名称、快照时间 |
| 任务调度 | `scheduler` | 调度任务 | 任务编号、任务名称、调度类型 |
| 缓存管理 | `cache` | 缓存配置 | 缓存编号、缓存目标、缓存策略 |
| 缓存重建批次包 | `cache_warmup` | 缓存目标/重建批次/缓存条目 | 数据版本、可命中参数范围、有效期 |
| 分析管道 | `pipeline` | 分析节点 | 节点编号、输入数据集、分析算子 |
| 分析模型 | `model` | 模型配置 | 模型编号、模型名称、模型类型 |
| 数据集市 | `data_mart` | 集市表 | 表编号、表名称、主题域 |
| 数据导出 | `export` | 导出任务 | 任务编号、导出对象、导出格式 |
| 数据审计 | `audit` | 审计记录 | 记录编号、操作类型、操作人 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 缓存重建批次包（cache_warmup）

在缓存配置登记「缓存目标数据集 + 策略」的基础上，补齐按真实 OLAP 查询结果批量预热的能力。
代码：`backend/app/services/cache_warmup.py`（领域规则）、
`backend/app/routers/cache_warmup.py`（接口）、
`frontend/src/views/cache_warmup/index.vue`（五个标签页的操作界面）。

核心规则：

1. **重建批次包**：`POST /api/cache-warmup/batches` 一次为多个目标按真实查询结果预热；
   共享输入数据集 `shared_sales` 有两个数据版本 v1（更新前，总计 100）与 v2（更新后，总计 120）。
2. **条目可追溯**：每条缓存条目保存实际结果、可命中参数范围（eq/in/range 谓词）、
   来源批次、数据版本与有效期（created_at/expires_at）。
3. **三道命中闸门**：`POST /api/cache-warmup/lookup` 必须同时满足参数范围匹配、
   数据版本一致、未过有效期；别的参数、别的版本、过期条目都不能命中。
4. **部分失败原子化**：同一批次某目标需生成条目数超过容量时，该目标整体不写入并标记
   「超出容量」，同包其它目标照常完成。
5. **联合查询同版对账**：`POST /api/cache-warmup/reconcile` 提供两种口径——
   - `valid_combo`：继续使用仍有效的完整旧组合（新版仅总计完成时明确返回更新前的 100）；
   - `same_batch_realtime`：以新版同一数据批次实时算出缺失分组（返回更新后的 120）。
   两种口径都声明服务版本，且只在同一版本内把分组求和对回总计；
   `GET /api/cache-warmup/mixed-guard` 显式展示跨版本混算被拦截。
6. **同组整组提交**：`POST /api/cache-warmup/groups/{组名}/submit` 支持缓存入口多选目标、
   一次提交，逐条返回已生成的数据版本与「暂未纳入」的目标。

验证方式：

```bash
# 后端领域规则 + HTTP 冒烟测试（仅依赖标准库）
cd backend && python3 -m unittest discover -s tests -v

# 端到端场景演示（需先启动后端，脚本会先 /reset 再走六个场景）
python3 scripts/demo_warmup.py
```
