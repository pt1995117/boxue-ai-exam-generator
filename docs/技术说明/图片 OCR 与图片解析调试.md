# 图片 OCR 与图片解析调试

本文档说明教材切片中的图片访问与 OCR 调试能力，包括适用场景、接口入参、返回结果和排障方法。

## 1. 适用场景

这组能力主要用于排查以下问题：

- 切片中图片是否已经被正确落盘
- 后端能否找到某张图片
- 当前图片模型配置是否可用
- 图片里是否包含表格、图表等需要额外处理的内容

## 2. 相关接口

当前有两个核心接口：

- `GET /api/{tenant}/slices/image`
- `POST /api/{tenant}/images/ocr-test`

代码参考：

- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L10591)
- [admin_api.py](/Users/panting/Desktop/搏学考试/AI出题/admin_api.py#L10636)

## 3. 图片访问接口

### 3.1 用途

`GET /api/{tenant}/slices/image` 用于直接取回图片文件本身，常见于前端预览或人工核对。

### 3.2 主要入参

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `path` | 是 | 图片路径或文件名 |
| `material_version_id` | 否 | 教材版本 ID |
| `template_id` | 否 | 出题模板 ID。若传模板，会优先使用模板绑定的教材版本 |

### 3.3 查找顺序

后端会依次尝试：

1. 绝对路径
2. 相对项目根目录的路径
3. 当前教材版本的图片目录下同名文件
4. 旧版 `extracted_images` 目录

### 3.4 白名单限制

即使文件存在，后端也只允许返回以下根目录下的图片：

- 项目内 `extracted_images`
- 租户目录下 `slices/images`

如果文件落在其他地方，会返回：

- `ACCESS_DENIED`

### 3.5 支持后缀

只支持：

- `.png`
- `.jpg`
- `.jpeg`
- `.webp`
- `.bmp`
- `.gif`

不支持时返回：

- `不支持的图片类型`

## 4. OCR 测试接口

### 4.1 用途

`POST /api/{tenant}/images/ocr-test` 是调试接口，用来验证：

- 当前请求有没有权限
- 图片路径能否被后端解析
- 图片模型配置是否生效
- 图片模型是否能返回分析文本

### 4.2 权限要求

该接口不是普通只读接口，需要：

- `material.upload`

没有权限时会返回：

- `无权限执行图片解析测试`

### 4.3 请求体

JSON 形态：

```json
{
  "path": "xxx.png",
  "material_version_id": "v20260401_100000"
}
```

也兼容从 query 参数读取同名字段，但建议统一走 JSON body。

### 4.4 返回字段

成功时常见返回：

- `ok`
- `analysis`
- `contains_table`
- `contains_chart`
- `config`

其中 `config` 主要用于排查当前加载到的图片模型配置，例如：

- `image_model`
- `image_provider`
- `image_base_url`
- `ark_project_name`
- `has_ark_api_key`
- `has_volc_ak_sk`

### 4.5 失败但 HTTP 200 的情况

如果图片模型调用失败，接口可能仍返回 `200`，但 payload 为：

- `ok = false`
- `error = ...`
- `config = ...`

这类情况说明：

- 接口路由通了
- 图片也找到了
- 但模型分析阶段失败

## 5. `contains_table` 与 `contains_chart`

调试接口会根据分析文本做两个辅助判断：

- `contains_table`
  - 是否抽到了表格内容
- `contains_chart`
  - 是否命中“坐标/曲线/趋势/图表/chart/axis”等关键词

说明：

- 这是便于排查的启发式判断，不是严格分类器
- 结果只能作为调试线索，不能当强业务结论

## 6. 常见报错与排查

### 6.1 `path is required`

说明：

- 没传图片路径

### 6.2 `MATERIAL_NOT_FOUND`

说明：

- 传了教材版本，但该版本不存在

### 6.3 `IMAGE_NOT_FOUND`

说明：

- 图片不在候选目录里
- 或路径/文件名写错

### 6.4 `ACCESS_DENIED`

说明：

- 图片虽然存在，但不在允许访问的根目录下

### 6.5 `不支持的图片类型`

说明：

- 后缀不在白名单里

### 6.6 `ok=false`

说明：

- 路由、权限、文件定位都没问题
- 是图片模型配置或调用本身失败

## 7. 调试建议

建议按以下顺序排查：

1. 先用 `slices` 列表接口确认切片是否真的挂了图片
2. 用 `slices/image` 直接访问图片，确认文件存在且能预览
3. 再调用 `images/ocr-test`
4. 若 `ok=false`，先检查 `config` 里的 provider、model、base_url、key 标记
5. 若返回 `contains_table=true` 或 `contains_chart=true`，再结合原图人工判断分析质量

## 8. 常见误区

- `slices/image` 只是取文件，不代表 OCR 一定可用
- `images/ocr-test` 成功不代表教材切片流程一定已经把图片解析结果回写到业务数据
- `ok=false` 不一定是权限问题，很多时候是模型配置或上游服务问题

