FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SITEFLOW_PLUGIN_MANIFEST=/plugin/current/plugin.json \
    SITEFLOW_PLUGIN_TOKEN_FILE=/run/secrets/plugin-token

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /plugin
COPY plugins/runtime/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY plugins/runtime/siteflow_plugin_runtime /plugin/siteflow_plugin_runtime
COPY proto/siteflow_proto /plugin/siteflow_proto
RUN useradd --create-home --uid 10001 siteflow \
    && mkdir -p /plugin/current /plugin/state /plugin/versions \
    && chown -R siteflow:siteflow /plugin/siteflow_plugin_runtime /plugin/siteflow_proto /plugin/state /plugin/versions

USER siteflow

EXPOSE 7000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-m", "siteflow_plugin_runtime.healthcheck"]

CMD ["python", "-m", "siteflow_plugin_runtime"]
