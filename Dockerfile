FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir app-store-server-library
# Install ffmpeg, nodejs, and Japanese TrueType fonts. We use IPAex
# (and IPA mincho) instead of fonts-noto-cjk because reportlab can only
# embed TrueType (glyf) fonts; Noto CJK ships as OpenType (CFF /
# postscript outlines), which TTFont rejects with "postscript outlines
# are not supported".
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    ca-certificates \
    fonts-ipafont-gothic \
    fonts-ipafont-mincho \
    fonts-ipaexfont-gothic \
    fonts-ipaexfont-mincho \
    && rm -rf /var/lib/apt/lists/*

# Install yt-dlp (System wide)
RUN pip install --no-cache-dir yt-dlp

COPY . /app

# サービスアカウントキーをコンテナ内に配置（外部に漏らさないよう .gitignore 済み）
# サービスアカウントキーをコンテナ内に配置（外部に漏らさないよう .gitignore 済み）
# COPY classnote-api-key.json /app/classnote-api-key.json
# ENV GOOGLE_APPLICATION_CREDENTIALS="/app/classnote-api-key.json"

ENV PORT=8080

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
