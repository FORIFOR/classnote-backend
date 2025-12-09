FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# サービスアカウントキーをコンテナ内に配置（外部に漏らさないよう .gitignore 済み）
COPY classnote-api-key.json /app/classnote-api-key.json
ENV GOOGLE_APPLICATION_CREDENTIALS="/app/classnote-api-key.json"

ENV PORT=8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
