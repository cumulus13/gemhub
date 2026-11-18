# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
ARG EXTRAS=postgres
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir build \
 && python -m build --wheel --outdir /dist . \
 && pip wheel --no-cache-dir --wheel-dir /wheels "/dist/$(ls /dist | head -n1)[${EXTRAS}]"

FROM python:3.12-slim
ARG EXTRAS=postgres
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GEMHUB_DATA_DIR=/data \
    GEMHUB_HOST=0.0.0.0 \
    GEMHUB_PORT=8080
RUN useradd --system --uid 10001 --create-home --home-dir /home/gemhub gemhub \
 && mkdir -p /data && chown gemhub:gemhub /data
COPY --from=build /wheels /wheels
RUN pip install --no-index "psycopg[binary]>=3.1" --find-links=/wheels /wheels/gemhub-*.whl \
 && rm -rf /wheels
# RUN chmod -R 775 /data
# USER gemhub
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GEMHUB_PORT','8080')+'/healthz',timeout=4)"
ENTRYPOINT ["gemhub"]
CMD ["serve"]
