FROM python:3.13.18-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .
# Runs as container-root on purpose: under rootless Docker that maps to the host user,
# so config backups/saves stay owned by that user and the rootless docker.sock is reachable.
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["python", "server.py"]
