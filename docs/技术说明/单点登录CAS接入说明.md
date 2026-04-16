# 单点登录（CAS）接入说明

本项目已支持 B 端通行证 CAS 单点登录，并与现有系统号权限模型联动。

## 1. 设计原则

- 用户身份以 `ucid` 为准。
- 一个 `ucid` 只绑定一个城市（`tenant_id`）。
- 同一城市可绑定多个系统号（`system_user`）。
- 系统号对应的角色/权限仍走现有 ACL 与权限矩阵。

## 2. 新增接口

- `GET /api/auth/meta`
  - 返回 SSO 是否启用、是否已登录、当前系统号、可切换系统号列表。
- `GET /api/auth/login?return_to=/path`
  - 跳转到 `login.ke.com/login`。
- `GET /api/auth/callback?ticket=...&rt=/path`
  - 校验 ST，建立本地 SSO 会话，回跳前端。
- `POST /api/auth/switch-system-user`
  - 在当前登录态下切换系统号。
- `GET|POST /api/auth/logout`
  - 清理本地会话并跳转 `login.ke.com/logout`。

## 3. 关键环境变量

- `SSO_ENABLED=true`
- `SSO_LOGIN_URL=https://login.ke.com/login`
- `SSO_LOGOUT_URL=https://login.ke.com/logout`
- `SSO_VALIDATE_URL=http://i.login.lianjia.com/serviceValidate`
- `SSO_SERVICE_BASE_URL=http://127.0.0.1:8600`
- `SSO_FRONTEND_BASE_URL=http://127.0.0.1:8522`
- `SSO_CALLBACK_PATH=/api/auth/callback`
- `SSO_COOKIE_NAME=boxue_sso_sid`
- `SSO_SESSION_TTL_SEC=28800`
- `SSO_COOKIE_SECURE=false`（HTTPS 正式环境建议 true）

## 4. 绑定文件

默认读取：

1. `.local/runtime/config/sso_user_bindings.json`（优先）
2. 仓库根目录 `sso_user_bindings.json`

格式可参考：

- `sso_user_bindings.json.example`

## 5. 注意事项

- `service` 必须与 `serviceValidate` 参数完全一致（含 query）。
- 回调 `ticket` 仅一次有效，且时效 10 秒。
- 建议使用 `businessToken(2.01)`，业务侧只维护本地会话与系统号上下文。
