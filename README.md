<div align="center">

# Lexfence

**AI-powered content moderation — multi-provider, self-hosted, open source.**

**English** · [中文](README_ZH.md)

⭐ If this project helps you, please **[Star it on GitHub](https://github.com/pymcn6/Lexfence-AI-Content-Moderation)** — it really helps!

</div>

---

### Features
- **Multi-provider AI channels**: OpenAI, OpenAI-compatible, Claude, Gemini. Add multiple channels, each with its own key, models and limits.
- **One-click model fetch** with format auto-fallback: tries the selected provider format, then falls back to other shapes (OpenAI `data:[{id}]`, AIHUBMIX `data:[{model_id}]`, Gemini `models:[{name}]`, plain arrays, etc.). Custom models endpoint supported.
- **Per-model controls**: priority, context window, `max_tokens`, daily token limit, rate limit, thinking mode. Batch enable/disable/delete, plus one-click **enable/pause an entire channel** (toggles all its models).
- **Priority fallback**: requests try models in priority order; on quota/rate/error it switches to the next.
- **Custom label sets & prompts**: define your own categories per scene; submitted prompts are AI-audited for malicious intent (audit never crashes when no AI is available — it approves by default).
- **REST API + Web console**: simple `result: true/false` or labelled responses.
- **User registration**: admin toggle, with three verification modes — none / email (SMTP, sent asynchronously) / admin approval.
- **Human verification (CAPTCHA)** on login & registration: built-in image CAPTCHA (anti-OCR distortion), Cloudflare Turnstile, hCaptcha, or Google reCAPTCHA.
- **Configurable branding**: site name, browser title, homepage description, favicon and logo — all from the admin console.
- **Update checker**: detects new GitHub releases, shows the changelog, supports a custom proxy prefix to speed up GitHub access, and gives Docker / git update instructions.
- **Demo mode**: isolated read-only showcase at `/demomode` with its own database.
- **i18n**: full English / 中文 interface with instant switching and auto language detection.
- **Easy install**: first-run wizard with **database choice** — auto-detect existing DB, or manually pick SQLite (zero config) or MySQL and fill in connection details (tested before install). Docker & docker-compose ready.

### Quick start (Python)
```bash
git clone https://github.com/pymcn6/Lexfence-AI-Content-Moderation.git
cd Lexfence-AI-Content-Moderation
pip install -r requirements.txt
cp .env.example .env        # optional: edit SECRET_KEY / DATABASE_URL
python app.py               # dev server at http://127.0.0.1:5000
# production: gunicorn -w 4 -b 0.0.0.0:5000 --timeout 180 app:app
```
Open the site — the **install wizard** guides you through database choice, admin account & site setup. Then add an AI channel under **AI Channels**.

### Quick start (Docker)

**Option A — one-line `docker run` (SQLite, zero dependencies):**
```bash
docker run -d --name lexfence -p 5000:5000 \
  -e SECRET_KEY=change-me-to-a-long-random-string \
  -v lexfence_data:/app/instance \
  ghcr.io/pymcn6/lexfence-ai-content-moderation:latest
```
- `-p 5000:5000` maps the container port to the host.
- `SECRET_KEY` — set your own long random string (if omitted, one is auto-generated and persisted under `/app/instance`).
- `-v lexfence_data:/app/instance` persists the SQLite DB, secret key and install lock, so your data survives container recreation.

**Option B — Docker Compose (recommended):**
```bash
docker compose up -d                      # app only, SQLite (zero dependencies)
docker compose --profile mysql up -d      # app + MySQL
docker compose --profile redis up -d      # app + Redis (rate-limit store)
```

The bundled `docker-compose.yml` defines three services (`app` always on; `db` and `redis` enabled on demand via profiles):

```yaml
services:
  app:
    # Use the published image (run `docker compose pull` to update).
    # To build locally instead, keep `build: .` and comment out the image line.
    image: ${LEXFENCE_IMAGE:-ghcr.io/pymcn6/lexfence-ai-content-moderation:latest}
    build: .
    container_name: lexfence
    restart: unless-stopped
    ports:
      - "5000:5000"                        # host:container, visit http://localhost:5000
    environment:
      # Session key: left empty, the container auto-generates a strong random key
      # and persists it to instance/secret_key. For multi-replica / scaled
      # deployments, set the SAME fixed random string (e.g. openssl rand -base64 48).
      SECRET_KEY: ${SECRET_KEY:-}
      # Default is SQLite. To use MySQL, uncomment the next line (or override via .env):
      # DATABASE_URL: mysql+pymysql://lexfence:lexfence@db:3306/lexfence?charset=utf8mb4
      DEFAULT_LOCALE: ${DEFAULT_LOCALE:-en}  # default UI language: en / zh
      # RATELIMIT_STORAGE_URI: redis://redis:6379/0   # enable when using the redis profile
    volumes:
      - ./instance:/app/instance           # persist SQLite DB, secret key, install lock

  db:                                       # MySQL — only starts with `--profile mysql`
    image: mysql:8.4
    container_name: lexfence-mysql
    profiles: ["mysql"]
    restart: unless-stopped
    environment:
      MYSQL_DATABASE: lexfence
      MYSQL_USER: lexfence
      MYSQL_PASSWORD: lexfence              # change this in production
      MYSQL_ROOT_PASSWORD: ${MYSQL_ROOT_PASSWORD:-rootpass}
    command: --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci
    volumes:
      - mysql_data:/var/lib/mysql           # persist MySQL data
    ports:
      - "3306:3306"                         # drop this mapping in production (internal access only)

  redis:                                    # Redis rate-limit store — only with `--profile redis`
    image: redis:7-alpine
    container_name: lexfence-redis
    profiles: ["redis"]
    restart: unless-stopped
    ports:
      - "6379:6379"

volumes:
  mysql_data:
```

Tip: put a `.env` file next to `docker-compose.yml` with `SECRET_KEY=...` (and `MYSQL_ROOT_PASSWORD=...` if using MySQL) — Compose loads it automatically. After startup, open **http://localhost:5000** to run the install wizard.

### Updating
```bash
# Docker: pull the latest published image
docker compose pull && docker compose up -d
# Source:
git pull && pip install -r requirements.txt   # then restart the service
```
The admin **Updates** page checks GitHub releases, shows the changelog, and lets you set a proxy prefix (e.g. `https://ghproxy.com/`) to accelerate access from restricted networks.

### API example
```bash
curl -X POST "http://localhost:5000/api/v1/detect" \
  -H "X-API-Key: YOUR_KEY" -H "Content-Type: application/json" \
  -d '{"text":"some text","scene":"message"}'
# -> {"result": false}
```

### Configuration
Only startup essentials live in `.env` (see `.env.example`): `SECRET_KEY`, optionally `DATABASE_URL` (or `MYSQL_*`). Everything else — AI channels, prompts, limits, branding, registration, CAPTCHA, SMTP, demo mode, update proxy — is managed in the web console and stored in the database (API keys and secrets are encrypted at rest).

### Notes
- **Image CAPTCHA fonts**: the Docker image bundles `fonts-dejavu`. For source installs, drop a `.ttf` into `assets/fonts/` for crisp captchas (see that folder's README).

### License
MIT © pymcn

---

<div align="center">

⭐ **[Star on GitHub](https://github.com/pymcn6/Lexfence-AI-Content-Moderation)** ⭐

</div>
