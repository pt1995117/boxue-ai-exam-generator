# 运行目录与 Git 卫生

为了避免运行后把仓库弄脏，项目现在默认将可变文件写到仓库内的忽略目录 `.local/`：

- 运行态根目录：`.local/runtime/`
- 运行态数据库：`.local/runtime/db/admin_p0.db`
- 运行态租户数据：`.local/runtime/data/<tenant>/...`
- 运行态配置：`.local/runtime/config/`
- 前端缓存：`.local/cache/admin-web/vite/`

## 兼容策略

- 新写入默认进入 `.local/runtime`
- 读取租户知识切片、母题等静态数据时，会先读 `.local/runtime/data`，没有再回退到仓库里的 `data/`
- 旧的 `tenant_users.json`、`填写您的Key.txt` 仍可读，但新的保存会优先写入 `.local/runtime/config/`

## 常用环境变量

- `BOXUE_RUNTIME_DIR`：运行态根目录
- `BOXUE_CACHE_DIR`：缓存根目录
- `BOXUE_KEY_FILE`：Key 配置文件路径
- `BOXUE_TENANT_USER_FILE`：租户 ACL 文件路径
- `DATABASE_URL`：数据库地址；未配置时默认使用 `.local/runtime/db/admin_p0.db`

## Git 防线

项目提供了提交前检查脚本，默认拦截以下文件进入提交：

- `.local/**`
- `logs/**`
- `node_modules/**`
- `admin-web/.vite/**`
- `*.db` / `*.sqlite*`
- `data/*/audit/**`
- `data/*/mapping/**`
- `data/*/slices/**`
- `data/*/exports/**`

安装方式：

```bash
bash tools/install_git_hooks.sh
```

## 建议工作流

1. 运行服务前，先执行 `bash tools/install_git_hooks.sh`
2. API Key 放在 `.local/runtime/config/填写您的Key.txt`
3. 运行后若 `git status` 仍出现 `data/`、`logs/`、`.vite/`，说明还有旧路径未迁移，应优先继续收口到运行目录
