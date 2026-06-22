<div align="center">

# Lexfence

**基于多渠道大模型的内容安全审核系统 · 可自部署 · 开源**

[English](README.md) · **中文**

⭐ 如果这个项目对你有帮助，欢迎 **[在 GitHub 点个 Star](https://github.com/pymcn6/Lexfence-AI-Content-Moderation)** 支持一下！

</div>

---

### 功能
- **多 AI 渠道**：支持 OpenAI、OpenAI 兼容、Claude、Gemini，可添加多个渠道，各自独立密钥、模型与限额。
- **一键拉取模型 + 格式自动回退**：先按所选渠道格式尝试，失败自动回退其它结构（OpenAI `data:[{id}]`、AIHUBMIX `data:[{model_id}]`、Gemini `models:[{name}]`、纯数组等），并支持自定义模型获取接口。
- **每模型精细控制**：优先级、上下文长度、`max_tokens`、每日 token 限额、速率限制、思考模式；支持**批量启用/禁用/删除**，以及一键**启用/暂停整条渠道**（同步开关其下全部模型）。
- **优先级回退**：按优先级依次尝试模型，遇额度/限速/错误自动切换下一个。
- **自定义标签集与提示词**：按场景定义自己的分类；提交的提示词经 AI 审核恶意意图（无可用 AI 时不会报错，默认放行）。
- **REST API + 网页后台**：返回简单的 `result: true/false` 或带标签结果。
- **用户注册**：后台开关，三种验证方式——无验证 / 邮箱验证（SMTP，后台线程异步发信）/ 管理员审核。
- **登录与注册人机验证**：内置图形验证码（抗 OCR 扭曲）、Cloudflare Turnstile、hCaptcha、Google reCAPTCHA。
- **可配置品牌**：站点名称、浏览器标题、首页介绍、Favicon、Logo，全部在后台管理。
- **更新检测**：检测 GitHub 新版本、显示更新日志、支持自定义代理前缀加速 GitHub，并给出 Docker / git 更新指引。
- **体验模式**：`/demomode` 提供独立数据库的只读演示。
- **国际化**：完整中 / 英界面，即时切换，自动识别浏览器语言。
- **安装简单**：首次安装向导支持**数据库选择**——自动检测已有库，或手动选 SQLite（零配置）/ MySQL 并填写连接信息（安装前测试连通）。提供 Docker / docker-compose。

### 快速开始（Python）
```bash
git clone https://github.com/pymcn6/Lexfence-AI-Content-Moderation.git
cd Lexfence-AI-Content-Moderation
pip install -r requirements.txt
cp .env.example .env        # 可选：修改 SECRET_KEY / DATABASE_URL
python app.py               # 开发服务器 http://127.0.0.1:5000
# 生产：gunicorn -w 4 -b 0.0.0.0:5000 --timeout 180 app:app
```
打开网站，**安装向导**会引导你选择数据库、创建管理员与站点信息；随后在「AI 渠道」中添加渠道。

### 快速开始（Docker）
```bash
docker compose up -d                      # 仅 app，SQLite
docker compose --profile mysql up -d      # app + MySQL
docker compose --profile redis up -d      # app + Redis（限流存储）
```

### 更新
```bash
# Docker：拉取最新发布镜像
docker compose pull && docker compose up -d
# 源码：
git pull && pip install -r requirements.txt   # 然后重启服务
```
后台「系统更新」页会检测 GitHub 发布、显示更新日志，并可设置代理前缀（如 `https://ghproxy.com/`）以在受限网络下加速访问。

### API 示例
```bash
curl -X POST "http://localhost:5000/api/v1/detect" \
  -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \
  -d '{"text":"some text","scene":"message"}'
# -> {"result": false}
```

### 配置说明
`.env` 只放启动必需项（见 `.env.example`）：`SECRET_KEY`、可选 `DATABASE_URL`（或 `MYSQL_*`）。其余配置（AI 渠道、提示词、限额、品牌、注册、验证码、SMTP、体验模式、更新代理）都在网页后台管理并存数据库（API 密钥与密钥项加密存储）。

### 说明
- **图形验证码字体**：Docker 镜像已内置 `fonts-dejavu`；源码部署可把 `.ttf` 放到 `assets/fonts/` 以获得清晰验证码（见该目录 README）。

### 许可证
MIT © pymcn6

---

<div align="center">

⭐ **[在 GitHub 点个 Star](https://github.com/pymcn6/Lexfence-AI-Content-Moderation)** ⭐

</div>
