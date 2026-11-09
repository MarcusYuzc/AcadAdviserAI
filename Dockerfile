# The public gateway. On AWS it runs on Fargate (ARM64) behind the load balancer.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src
RUN python -m pip install --no-cache-dir /app \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin gateway

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"]
# Keep-alive outlasts the load balancer's 360 s idle timeout, so it never reuses a closed connection.
CMD ["python", "-m", "uvicorn", "acadadviser.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-keep-alive", "400"]
