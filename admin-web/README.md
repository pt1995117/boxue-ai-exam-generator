# Admin Web (Ant Design Pro Style)

这是迁移第一阶段的 React 管理后台骨架，包含：

- 工作台
- 切片核对（筛选、勾选、批量审核）
- 映射确认（筛选、勾选、批量确认）

## 目录

- `src/layouts/AdminLayout.jsx`：后台壳布局
- `src/pages/SliceReviewPage.jsx`：切片核对页
- `src/pages/MappingReviewPage.jsx`：映射确认页
- `src/services/api.js`：API 封装

## 依赖安装

当前环境若无外网，`npm install` 会失败。请在可访问 npm 的环境执行：

```bash
npm --prefix admin-web install
```

## 启动

1. 启动后端 API（Flask）：

```bash
python admin_api.py
```

2. 启动前端：

```bash
npm --prefix admin-web run dev
```

默认前端端口：`8522`，已代理 `/api` 到 `http://127.0.0.1:8600`。

说明：

- Vite 缓存默认写入项目根目录下的 `.local/cache/admin-web/vite/`
- 该目录已被 Git 忽略，用于避免前端开发缓存污染工作区

## 已实现 API

- `GET /api/tenants`
- `GET /api/{tenant}/slices`
- `POST /api/{tenant}/slices/review/batch`
- `GET /api/{tenant}/mappings`
- `POST /api/{tenant}/mappings/review/batch`
