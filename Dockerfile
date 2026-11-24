# Bản bookworm đầy đủ có sẵn git (Compiler lưu L2 trong repo git mỗi tenant), khỏi cần apt vốn không tải được từ mạng nội bộ.
# Mạng nội bộ không ra Internet thì truyền --build-arg BASE_IMAGE=<registry nội bộ>/python:3.12-bookworm
ARG BASE_IMAGE=python:3.12-bookworm
FROM ${BASE_IMAGE}

WORKDIR /app
# Mạng nội bộ có mirror PyPI thì truyền PIP_INDEX_URL/PIP_TRUSTED_HOST (compose đọc từ .env).
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org
COPY requirements.txt .
# Cache mount giữ gói đã tải giữa các lần build, vì mạng hay đứt giữa chừng.
RUN --mount=type=cache,target=/root/.cache/pip pip install --timeout 120 --retries 10 -r requirements.txt
# kubectl cho công cụ kubectl_* của template k8s; mạng không tải được dl.k8s.io thì truyền KUBECTL_URL trỏ mirror nội bộ.
# Đặt trước COPY mã để đổi mã không phải tải lại 50MB.
ARG KUBECTL_URL=https://dl.k8s.io/release/v1.29.3/bin/linux/amd64/kubectl
ADD ${KUBECTL_URL} /usr/local/bin/kubectl
RUN chmod 755 /usr/local/bin/kubectl && kubectl version --client
# helm cho công cụ helm_*; helm 3.16 không đọc được platformHooks trong plugin.yaml của helm-diff 3.15, nên dùng 3.19. URL để trống thì bỏ qua (helm hoặc plugin), công cụ báo "chưa cài" khi được gọi.
# Không dùng `helm plugin install` vì hook cài đặt tải tiếp từ GitHub; plugin.yaml của drift không có trong tarball nên viết tay.
ARG HELM_URL=https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz
ARG HELM_DIFF_URL=
ARG HELM_DRIFT_URL=
RUN t=$(mktemp -d) && mkdir -p /opt/helm/plugins \
    && if [ -n "$HELM_URL" ]; then curl -fsSL --retry 5 -o "$t/helm.tgz" "$HELM_URL" && tar xzf "$t/helm.tgz" -C "$t" \
         && install -m 755 "$t/linux-amd64/helm" /usr/local/bin/helm && helm version --short; fi \
    && if [ -n "$HELM_DIFF_URL" ]; then curl -fsSL --retry 5 -o "$t/diff.tgz" "$HELM_DIFF_URL" && tar xzf "$t/diff.tgz" -C /opt/helm/plugins \
         && test -x /opt/helm/plugins/diff/bin/diff; fi \
    && if [ -n "$HELM_DRIFT_URL" ]; then mkdir -p "$t/drift" /opt/helm/plugins/drift/bin && curl -fsSL --retry 5 -o "$t/drift.tgz" "$HELM_DRIFT_URL" \
         && tar xzf "$t/drift.tgz" -C "$t/drift" && install -m 755 "$(find "$t/drift" -type f -name helm-drift | head -n 1)" /opt/helm/plugins/drift/bin/helm-drift \
         && printf 'name: "drift"\nversion: "0.1.2"\nusage: "Identifies configurations deviated from helm charts"\ndescription: "helm-drift"\nuseTunnel: true\ncommand: "$HELM_PLUGIN_DIR/bin/helm-drift"\n' > /opt/helm/plugins/drift/plugin.yaml; fi \
    && if [ -x /usr/local/bin/helm ]; then HELM_PLUGINS=/opt/helm/plugins helm plugin list; fi \
    && rm -rf "$t"
COPY aiops ./aiops

# /data tạo sẵn với chủ sở hữu aiops để named volume nhận đúng quyền khi khởi tạo lần đầu.
RUN useradd --system --uid 10001 --home-dir /app aiops && install -d -o aiops -g aiops -m 750 /data
USER aiops
ENV AIOPS_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# Một worker: lịch nền và khóa Compiler nằm trong tiến trình.
CMD ["uvicorn", "aiops.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
