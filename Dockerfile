# eFootball Champions League (Django). Runs on Koyeb, Railway, Fly.io, Render or any Docker host.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=3000 TRUST_PROXY=1 DB_FILE=/data/league.sqlite3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# /data only survives restarts if the host mounts a volume there. Otherwise use DATABASE_URL (Postgres).
RUN mkdir -p /data && sed -i 's/\r$//' start.sh
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT','3000'), timeout=4)"
CMD ["sh", "start.sh"]
