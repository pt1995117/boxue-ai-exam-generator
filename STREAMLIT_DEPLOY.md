# 🚀 Streamlit Cloud 部署指南

## 快速部署步骤

### 1. 访问 Streamlit Cloud
打开浏览器访问：https://share.streamlit.io/

### 2. 使用 GitHub 账号登录
- 点击 "Sign in with GitHub"
- 授权 Streamlit Cloud 访问您的 GitHub 账号

### 3. 部署应用
1. 点击 "New app" 按钮
2. 填写部署信息：
   - **Repository**: `pt1995117/boxue-ai-exam-generator`
   - **Branch**: `main`
   - **Main file path**: `app.py`
3. 点击 "Deploy!"

### 4. 配置环境变量（重要！）
部署后，在应用设置中添加以下 Secrets：

#### 方法一：在 Streamlit Cloud 界面配置
1. 进入应用设置（Settings）
2. 点击 "Secrets" 标签
3. 添加以下环境变量：

```toml
OPENAI_API_KEY = "你的OpenAI或DeepSeek API Key"
# 或
GEMINI_API_KEY = "你的Gemini API Key"
```

#### 方法二：使用 secrets.toml（推荐）
在 Streamlit Cloud 的 Secrets 页面，直接粘贴：

```toml
OPENAI_API_KEY = "sk-你的密钥"
GEMINI_API_KEY = "你的Gemini密钥"
OPENAI_BASE_URL = "https://api.deepseek.com"
OPENAI_MODEL = "deepseek-chat"
```

### 5. 等待部署完成
- 首次部署可能需要 3-5 分钟
- 部署完成后，您会获得一个类似这样的链接：
  ```
  https://boxue-ai-exam-generator.streamlit.app
  ```

## 📝 注意事项

1. **API Key 安全**：
   - ✅ 使用 Streamlit Cloud 的 Secrets 功能存储 API Key
   - ❌ 不要将 API Key 直接写在代码中
   - ❌ 不要提交包含真实 Key 的文件到 GitHub

2. **文件访问**：
   - 确保 `bot_knowledge_base.jsonl` 等数据文件已提交到 GitHub
   - 如果文件太大，考虑使用 Git LFS

3. **首次运行**：
   - 首次加载知识库可能需要一些时间
   - 建议在 README 中说明

## 🔗 部署后的链接格式

您的应用链接将是：
```
https://[app-name]-[username].streamlit.app
```

例如：
```
https://boxue-ai-exam-generator-pt1995117.streamlit.app
```

## 🎉 完成！

部署完成后，您就可以通过网页链接分享给其他人使用了！
