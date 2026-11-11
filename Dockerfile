# Bản bookworm đầy đủ có sẵn git (Compiler lưu L2 trong repo git mỗi tenant), khỏi cần apt vốn không tải được từ mạng nội bộ.
# Mặc định kéo qua registry nội bộ; ngoài mạng này: --build-arg BASE_IMAGE=python:3.12-bookworm
ARG BASE_IMAGE=10.60.129.132:8890/python:3.12-bookworm
FROM ${BASE_IMAGE}

WORKDIR /app
# Ở mạng nội bộ, compose trỏ pip vào Nexus vì đường Internet trực tiếp tới PyPI rất chậm.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org
COPY requirements.txt .
# Cache mount giữ gói đã tải giữa các lần build, vì mạng hay đứt giữa chừng.
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 120 --retries 10 -r requirements.txt
COPY kagent ./kagent

# /data tạo sẵn với chủ sở hữu kagent để named volume nhận đúng quyền khi khởi tạo lần đầu.
RUN useradd --system --uid 10001 --home-dir /app kagent && install -d -o kagent -g kagent -m 750 /data
USER kagent
ENV KAGENT_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# Một worker: lịch nền và khóa Compiler nằm trong tiến trình.
CMD ["uvicorn", "kagent.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
