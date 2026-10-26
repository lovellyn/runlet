"""Small, environment-driven configuration."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    root: Path = Path("var")
    git_user_name: str = ""
    git_user_email: str = ""
    max_jobs: int = 2
    max_workspaces: int = 32
    max_job_records: int = 256
    default_timeout: float = 300
    max_timeout: float = 3600
    max_output_bytes: int = 1_048_576
    response_bytes: int = 65_536
    max_file_bytes: int = 1_048_576
    setup_timeout: float = 180
    public_base_url: str = ""
    export_ttl_seconds: int = 900
    max_export_bytes: int = 268_435_456
    max_exports: int = 16
    allowed_hosts: tuple[str, ...] = (
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
    )
    allowed_origins: tuple[str, ...] = (
        "http://localhost",
        "http://localhost:*",
        "http://127.0.0.1",
        "http://127.0.0.1:*",
    )

    def __post_init__(self) -> None:
        if not self.token or self.token != self.token.strip():
            raise ValueError(
                "Set SANDBOX_TOKEN to a nonempty shared token without outer whitespace"
            )
        for key in (
            "max_jobs",
            "max_workspaces",
            "max_job_records",
            "default_timeout",
            "max_timeout",
            "max_output_bytes",
            "response_bytes",
            "max_file_bytes",
            "setup_timeout",
            "export_ttl_seconds",
            "max_export_bytes",
            "max_exports",
        ):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be finite and positive")
        if self.default_timeout > self.max_timeout:
            raise ValueError("default_timeout must not exceed max_timeout")
        if self.response_bytes > self.max_output_bytes:
            raise ValueError("response_bytes must not exceed max_output_bytes")
        if self.public_base_url:
            parsed = urlsplit(self.public_base_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or "?" in self.public_base_url
                or "#" in self.public_base_url
                or "\\" in self.public_base_url
                or any(
                    character.isspace() or ord(character) < 32 for character in self.public_base_url
                )
            ):
                raise ValueError(
                    "PUBLIC_BASE_URL must be an HTTP(S) URL without credentials/query/fragment"
                )
            # Accessing port also validates malformed or out-of-range port numbers.
            _ = parsed.port

    @classmethod
    def from_env(cls) -> "Settings":
        hosts = list(cls.__dataclass_fields__["allowed_hosts"].default)
        origins = list(cls.__dataclass_fields__["allowed_origins"].default)
        hosts += [v.strip() for v in os.getenv("ALLOWED_HOSTS", "").split(",") if v.strip()]
        origins += [v.strip() for v in os.getenv("ALLOWED_ORIGINS", "").split(",") if v.strip()]
        return cls(
            token=os.getenv("SANDBOX_TOKEN", ""),
            root=Path(os.getenv("SANDBOX_ROOT", "var")).resolve(),
            git_user_name=os.getenv("GIT_USER_NAME", ""),
            git_user_email=os.getenv("GIT_USER_EMAIL", ""),
            max_jobs=int(os.getenv("MAX_CONCURRENT_JOBS", "2")),
            max_workspaces=int(os.getenv("MAX_WORKSPACES", "32")),
            max_job_records=int(os.getenv("MAX_JOB_RECORDS", "256")),
            default_timeout=float(os.getenv("DEFAULT_JOB_TIMEOUT", "300")),
            max_timeout=float(os.getenv("MAX_JOB_TIMEOUT", "3600")),
            max_output_bytes=int(os.getenv("MAX_OUTPUT_BYTES", "1048576")),
            response_bytes=int(os.getenv("RESPONSE_BYTES", "65536")),
            max_file_bytes=int(os.getenv("MAX_FILE_BYTES", "1048576")),
            setup_timeout=float(os.getenv("SETUP_TIMEOUT", "180")),
            public_base_url=os.getenv("PUBLIC_BASE_URL", ""),
            export_ttl_seconds=int(os.getenv("EXPORT_TTL_SECONDS", "900")),
            max_export_bytes=int(os.getenv("MAX_EXPORT_BYTES", "268435456")),
            max_exports=int(os.getenv("MAX_EXPORTS", "16")),
            allowed_hosts=tuple(hosts),
            allowed_origins=tuple(origins),
        )
