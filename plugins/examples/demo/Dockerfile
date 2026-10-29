FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SITEFLOW_PLUGIN_ID=demo \
    SITEFLOW_PLUGIN_MANIFEST=/plugin/current/plugin.json \
    SITEFLOW_PLUGIN_FEATURES_FILE=/plugin/current/features.json \
    SITEFLOW_PLUGIN_CAPTCHA_METHODS_FILE=/plugin/current/captcha_methods.json \
    SITEFLOW_PLUGIN_TOKEN_FILE=/run/secrets/plugin-token \
    SITEFLOW_PLUGIN_APP_PATH=/plugin/app \
    SITEFLOW_PLUGIN_HANDLER=demo_plugin:execute_step \
    SITEFLOW_PLUGIN_CAPTCHA_HANDLER=demo_plugin:recognize_captcha \
    SITEFLOW_PLUGIN_CREDENTIAL_HANDLER=demo_plugin:resolve_credential \
    SITEFLOW_PLUGIN_TOTP_HANDLER=demo_plugin:generate_totp

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /plugin
COPY plugins/runtime/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY plugins/runtime/siteflow_plugin_runtime /plugin/siteflow_plugin_runtime
COPY proto/siteflow_proto /plugin/siteflow_proto
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" Pillow numpy
COPY plugins/examples/demo/plugin.json /plugin/current/plugin.json
COPY plugins/examples/demo/features.json /plugin/current/features.json
COPY plugins/examples/demo/captcha_methods.json /plugin/current/captcha_methods.json
COPY plugins/examples/demo/demo_plugin.py /plugin/app/demo_plugin.py
COPY worker/nexusphp_gd5 /plugin/app/nexusphp_gd5

RUN useradd --create-home --uid 10001 siteflow \
    && mkdir -p /plugin/state /plugin/versions \
    && chown -R siteflow:siteflow /plugin

USER siteflow

EXPOSE 7000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-m", "siteflow_plugin_runtime.healthcheck"]

CMD ["python", "-m", "siteflow_plugin_runtime"]
