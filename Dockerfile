# Multi-stage so the runtime image carries no build toolchain.
FROM python:3.13-slim AS build
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.13-slim
# Trading containers get restarted by orchestrators; unbuffered output means the
# last log line before a kill actually reaches the log sink.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=build /install /usr/local
COPY quant/ ./quant/
COPY configs/ ./configs/
COPY README.md ./

# Never run a trading process as root.
RUN useradd --create-home --uid 10001 quant \
 && mkdir -p /data && chown -R quant:quant /app /data
USER quant

ENV DB_PATH=/data/quant_state.db HOST=0.0.0.0 PORT=8000 LOG_FORMAT=json
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/api/health',timeout=4).status==200 else 1)"

# Default to the dashboard. Override the command for a trading worker:
#   docker run ... quant-engine python -m quant dryrun configs/live_crypto.yaml --state /data/quant_state.db
CMD ["sh", "-c", "python -m quant serve configs/demo.yaml --host $HOST --port $PORT --state $DB_PATH"]
