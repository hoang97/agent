"""Cấu hình đọc từ biến môi trường; test gọi settings.configure() để đổi thư mục dữ liệu."""
from __future__ import annotations

import os
from pathlib import Path


class Settings:
    def __init__(self) -> None:
        self.configure()

    def configure(self, **overrides) -> None:
        env = os.environ.get
        self.data_dir = Path(env("KAGENT_DATA_DIR", "data"))
        self.anthropic_key = env("ANTHROPIC_API_KEY", "")
        self.anthropic_auth_token = env("ANTHROPIC_AUTH_TOKEN", "")
        self.anthropic_base_url = env("ANTHROPIC_BASE_URL", "")
        self.secret_key = env("KAGENT_SECRET_KEY", "")
        self.llm_provider = env("KAGENT_LLM", "auto")  # auto | anthropic | heuristic
        self.model_strong = env("KAGENT_MODEL_STRONG", "claude-opus-5-5")
        self.model_fast = env("KAGENT_MODEL_FAST", "claude-haiku-5-5")
        self.admin_password = env("KAGENT_ADMIN_PASSWORD", "")
        self.demo_password = env("KAGENT_DEMO_PASSWORD", "")
        self.cookie_secure = env("KAGENT_COOKIE_SECURE", "0") == "1"
        self.scheduler_enabled = env("KAGENT_SCHEDULER", "1") == "1"
        self.scheduler_interval = int(env("KAGENT_SCHEDULER_INTERVAL", "60"))
        self.learn_batch_n = int(env("KAGENT_LEARN_N", "5"))
        self.session_hours = 12
        self.approval_ttl_minutes = 30
        self.grant_ttl_minutes = 60
        # Embedding băm chỉ phản ánh trùng từ vựng nên ngưỡng thấp hơn mức 0,85 của tài liệu kiến trúc.
        self.near_threshold = 0.55
        self.confirm_threshold = 0.9
        self.budget_core = 1500
        self.budget_playbook = 3000
        self.budget_ku = 2000
        self.max_agent_steps = 8
        for key, value in overrides.items():
            setattr(self, key, value)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "kagent.db"

    @property
    def knowledge_dir(self) -> Path:
        return self.data_dir / "knowledge"


settings = Settings()
