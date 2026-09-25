# 3MF Manager 运行时镜像：零第三方依赖，纯 Python 标准库（基础镜像用本地常见缓存的 3.13-slim）
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    MFMANAGER_HOST=0.0.0.0 \
    MFMANAGER_DATA_DIR=/data \
    MFMANAGER_CONFIG=/data/config.json

WORKDIR /app

# 运行时只需这些模块与静态资源；不拷贝 config.json（含密钥，属数据盘内容）
COPY server.py llm_client.py parse_3mf.py merge_3mf.py subcat.py mc_subcat.py \
     classify.py db.py version.py relate.py runner.py rules.json config.example.json ./
COPY static/ ./static/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && useradd --uid 1000 --create-home app \
    && mkdir -p /data \
    && chown -R app:app /data /app

USER app

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/', timeout=4)" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
