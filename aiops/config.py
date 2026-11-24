"""Cấu hình đọc từ biến môi trường; test gọi settings.configure() để đổi thư mục dữ liệu."""
from __future__ import annotations

import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.configure()

    def configure(self, **overrides) -> None:
        env = os.environ.get
        self.data_dir = Path(env("AIOPS_DATA_DIR", "data"))
        self.anthropic_key = env("ANTHROPIC_API_KEY", "")
        self.anthropic_auth_token = env("ANTHROPIC_AUTH_TOKEN", "")
        self.anthropic_base_url = env("ANTHROPIC_BASE_URL", "")
        self.secret_key = env("AIOPS_SECRET_KEY", "")
        self.llm_provider = env("AIOPS_LLM", "auto")  # auto | anthropic | heuristic
        self.model_strong = env("AIOPS_MODEL_STRONG", "claude-opus-5-5")
        self.model_fast = env("AIOPS_MODEL_FAST", "claude-haiku-5-5")
        self.admin_password = env("AIOPS_ADMIN_PASSWORD", "")
        self.demo_password = env("AIOPS_DEMO_PASSWORD", "")
        self.cookie_secure = env("AIOPS_COOKIE_SECURE", "0") == "1"
        self.scheduler_enabled = env("AIOPS_SCHEDULER", "1") == "1"
        self.scheduler_interval = int(env("AIOPS_SCHEDULER_INTERVAL", "60"))
        self.learn_batch_n = int(env("AIOPS_LEARN_N", "5"))
        self.session_hours = 12  # phiên hết hạn sau ngần này giờ không hoạt động; đang dùng thì tự gia hạn
        self.session_max_days = 30  # tuổi tối đa của một phiên kể cả khi vẫn hoạt động
        self.approval_ttl_minutes = 30
        self.grant_ttl_minutes = 60
        # Embedding băm chỉ phản ánh trùng từ vựng nên ngưỡng thấp hơn mức 0,85 của tài liệu kiến trúc.
        self.near_threshold = 0.55
        self.confirm_threshold = 0.9
        # Ứng viên chưa từng được duyệt sau 4 ngày tuổi (số liệu 11-19/11), nên quá 7 ngày coi như bị bỏ quên.
        self.lint_stale_days = int(env("AIOPS_LINT_STALE_DAYS", "7"))
        self.incident_learn = env("AIOPS_INCIDENT_LEARN", "1") == "1"  # sự cố đã xử lý xong thành nguồn tri thức (sources.py)
        self.budget_core = 1500
        self.budget_playbook = 3000
        self.budget_ku = 2000
        self.knowledge_dedup = env("AIOPS_KNOWLEDGE_DEDUP", "1") == "1"  # không gửi lại tri thức đã có trong lịch sử hội thoại
        self.budget_history = 24000  # lịch sử thô gồm cả kết quả công cụ; phần lớn được đọc từ prompt cache
        self.max_agent_steps = 12  # bước đã rẻ nhờ _filter và cache; hết bước thì model buộc phải tổng kết
        self.max_subagent_steps = 6
        for key, value in overrides.items():
            setattr(self, key, value)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "aiops.db"

    @property
    def knowledge_dir(self) -> Path:
        return self.data_dir / "knowledge"


settings = Settings()
