FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /demo
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" Pillow numpy
COPY worker/nexusphp_gd5 /demo/nexusphp_gd5
COPY plugins/examples/demo/demo_site.py /demo/demo_site.py
USER 10001:10001
EXPOSE 8099
CMD ["python", "demo_site.py"]
