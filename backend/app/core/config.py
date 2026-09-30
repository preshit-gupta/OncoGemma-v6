import os
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENV: str = os.getenv("ENV", "dev")
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "dev")
    DEBUG: bool = True
    
    # GCP
    GCP_PROJECT_ID: str = os.getenv("GCP_PROJECT_ID", "oncogemma")
    GCP_REGION: str = os.getenv("GCP_REGION", "us-central1")
    USE_REAL_GCS: bool = os.getenv("USE_REAL_GCS", "true").lower() in ("true", "1")
    
    # Vertex AI Endpoint Configuration - Path Foundation (Stage 3)
    VERTEX_PATH_FOUNDATION_ENDPOINT_ID: str = os.getenv(
        "VERTEX_PATH_FOUNDATION_ENDPOINT_ID",
        "6638504312592924672"
    )
    VERTEX_PATH_FOUNDATION_LOCATION: str = os.getenv(
        "VERTEX_PATH_FOUNDATION_LOCATION",
        "us-central1"
    )

    # Vertex AI Endpoint Configuration - MedGemma 1.5 (Stage 5 Grading)
    VERTEX_MEDGEMMA_ENDPOINT_ID: str = os.getenv(
        "VERTEX_MEDGEMMA_ENDPOINT_ID",
        "2026818294165536768"
    )
    VERTEX_MEDGEMMA_LOCATION: str = os.getenv(
        "VERTEX_MEDGEMMA_LOCATION",
        "us-central1"
    )
    # Model versions, generation settings and retries live in configs/models.yaml.

    # Vertex AI Endpoint Configuration - YOLO Mitosis Sweeper (Stage 4)
    VERTEX_MITOSIS_ENDPOINT_ID: str | None = os.getenv(
        "VERTEX_MITOSIS_ENDPOINT_ID",
        None
    )
    VERTEX_MITOSIS_LOCATION: str = os.getenv(
        "VERTEX_MITOSIS_LOCATION",
        "us-central1"
    )

    # Gemini Multimodal Referee Configuration (Stage 4)
    GEMINI_REFEREE_MODEL: str = os.getenv("GEMINI_REFEREE_MODEL", "gemini-2.5-flash")

    # Database: set exactly one of DATABASE_URL or CLOUD_SQL_CONNECTION_NAME (app.core.db).
    # The Cloud SQL password is read only from the DB_PASSWORD environment variable
    # (Secret Manager on Cloud Run), never from a setting or a default.
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    CLOUD_SQL_CONNECTION_NAME: str = os.getenv("CLOUD_SQL_CONNECTION_NAME", "")
    DB_USER: str = os.getenv("DB_USER", "oncogemma")
    DB_NAME: str = os.getenv("DB_NAME", "oncogemma_db")

    # GCS Configuration
    GCS_RAW_BUCKET: str = os.getenv("GCS_RAW_BUCKET", "oncogemma-dev-raw")
    GCS_PYRAMIDS_BUCKET: str = os.getenv("GCS_PYRAMIDS_BUCKET", "oncogemma-dev-pyramids")
    GCS_ARTIFACTS_BUCKET: str = os.getenv("GCS_ARTIFACTS_BUCKET", "oncogemma-dev-artifacts")
    CDN_BASE_URL: str | None = os.getenv("CDN_BASE_URL", None)
    STORAGE_EMULATOR_HOST: str | None = os.getenv("STORAGE_EMULATOR_HOST", None)

    # Cloud Tasks & Asynchronous Cloud Workers
    USE_CLOUD_TASKS: bool = os.getenv("USE_CLOUD_TASKS", "false").lower() in ("true", "1")
    CLOUD_TASKS_LOCATION: str = os.getenv("CLOUD_TASKS_LOCATION", os.getenv("GCP_REGION", "us-central1"))
    CLOUD_TASKS_QUEUE: str = os.getenv("CLOUD_TASKS_QUEUE", "oncogemma-stage-queue")
    WORKER_SERVICE_URL: str = os.getenv("WORKER_SERVICE_URL", "http://localhost:8000")
    CLOUD_TASKS_SERVICE_ACCOUNT: str = os.getenv("CLOUD_TASKS_SERVICE_ACCOUNT", "")
    # Polling worker started by the API lifespan (app.main). The test suite turns it off.
    RUN_IN_PROCESS_WORKER: bool = True
    
    # Sign-in (SPEC-03 §3). Google verifies ID tokens for this OAuth web client ID.
    # The session signing key is read only from the SESSION_SIGNING_KEY environment variable
    # (Secret Manager og-session-signing-key on Cloud Run), never from a setting (app.auth.sessions).
    GOOGLE_OAUTH_CLIENT_ID: str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    # Comma-separated Google Workspace domains whose accounts may sign in (the ID token's hd claim).
    AUTH_ALLOWED_DOMAINS: str = os.getenv("AUTH_ALLOWED_DOMAINS", "")
    # Signing in with this email makes it an admin while no active admin exists (SPEC-03 §3.3, §8).
    BOOTSTRAP_ADMIN_EMAIL: str = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "")
    
    # Config directory
    CONFIGS_DIR: str = os.path.join(os.path.dirname(__file__), "../../../configs")
    
    # CORS Configuration (#3)
    CORS_ORIGINS: str = os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000")


settings = Settings()
