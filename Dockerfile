FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 系统依赖（cryptography 编译、Pillow 图像库、验证码字体）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libffi-dev \
    libjpeg-dev zlib1g-dev \
    fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据/实例目录（SQLite、密钥、锁文件）
RUN mkdir -p instance
VOLUME ["/app/instance"]

EXPOSE 5000

# 生产用 gunicorn 启动；首次访问会进入 /install 安装向导
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "--timeout", "180", "app:app"]
