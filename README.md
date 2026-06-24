<div align="center">

# Lexfence

**AI-powered content moderation — multi-provider, self-hosted, open source.**

**English** · [中文](README_ZH.md)

⭐ If this project helps you, please **[Star it on GitHub](https://github.com/pymcn6/Lexfence-AI-Content-Moderation)** — it really helps!

**[🚀 Live Demo](https://textsafe.pym.plus/demomode)** — try it instantly, no signup needed.

💛 **Like it? [Sponsor the project](sponsor.md)** — your support keeps it going.

</div>

---

### Features
- **Multi-modal moderation**: review text, images and video in one place.
- **Multi-provider AI channels**: connect and manage OpenAI, Claude, Gemini and compatible services.
- **Custom prompts & labels**: define your own prompt templates and categories per scene.
- **REST API**: synchronous and asynchronous calls (async avoids timeouts on long-running jobs).
- **Commercialization ready**: a public landing page, a built-in **pricing page** that renders your configured price per 1M tokens (text / image / video) with multi-currency and custom exchange-rate support, plus an embeddable **recharge page** (iframe your own card/payment site) and redemption codes for top-ups.
- **API key controls**: per-key usage limits (tokens per minute/hour/day/month/year), request-rate limits, expiry, and per-key usage stats; per-user key quota with an admin-configurable site contact shown when the limit is reached.
- **Token-based billing**: usage metered by tokens, billed by actual consumption.
- **Admin tools**: user management, quota management, detection logs and a data dashboard.
- **Demo mode**: an online showcase to try the system instantly.
- **i18n**: full English / 中文 interface.
- **Easy deploy**: one-command Docker / docker-compose setup.

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

### Publishing a release (web, v2.4.0)
1. Commit your changes locally and push the branch.
2. Create the tag and push it: `git tag v2.4.0 && git push origin v2.4.0`.
3. On GitHub, open **Releases → Draft a new release**, choose tag `v2.4.0`, write the notes, and **Publish**. The bundled GitHub Actions workflow then builds and pushes the Docker image to GHCR automatically.

**Do NOT upload these** (already covered by `.gitignore`): `.env`, the whole `instance/` folder (SQLite DB, `secret_key`, install lock), any `*.db` / `*.sqlite3`, and `__pycache__/`. Only commit `.env.example` (placeholders only).

### Sponsor
Like the project? See the [sponsor page](sponsor.md) — thanks for your support!

### License
MIT © pymcn

---

<div align="center">

⭐ **[Star on GitHub](https://github.com/pymcn6/Lexfence-AI-Content-Moderation)** ⭐

</div>
