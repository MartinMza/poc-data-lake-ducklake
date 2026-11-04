"""Configuración de la aplicación: pydantic-settings, una sola fuente de verdad.

Todas las variables de entorno se declaran como campos tipados de `Settings`:
una variable que no está acá no es configuración de la aplicación.

Dos reglas de diseño:

1. **Granularidad.** Ninguna variable mezcla host y puerto, ni esquema, bucket y
   prefijo. Los valores combinados (DSN del catálogo, endpoint de S3, ruta del
   lago) son *derivados* en propiedades, no entradas editables.
2. **Secretos como `SecretStr`.** Una contraseña o un secreto nunca aparece en
   un `repr()`, en un log ni en un mensaje de error: hay que pedir
   `get_secret_value()` explícitamente.

`get_settings()` **no** cachea a propósito. Cachear una configuración tiene un
modo de falla silencioso: alguien cambia el entorno (o el `.env`) y el proceso
sigue usando valores viejos sin avisar. El costo de leerla es una construcción
de `Settings` (26 campos + parseo del `.env`), despreciable frente a una
request. Si alguna vez importa, cacheadla en el composition root y invalidarla
explícitamente.
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)

# Valores que aparecen como default de ejemplo en el repo. Contra un host
# público son una credencial adivinable; contra un host interno (local, Docker,
# red privada) son parte del PoC.
DEFAULT_INSECURE_SECRETS = frozenset(
    {"", "ducklake", "password", "changeme", "secret", "admin"}
)
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})

_DEFAULT_GARAGE_KEY_ID = "GK0123456789abcdef0123456789abcdef"
_DEFAULT_GARAGE_SECRET = (
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
)


def _split_csv(value: Any) -> list[str]:
    """Normaliza una lista separada por comas (acepta str, list o tuple)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value)]


class Settings(BaseSettings):
    """Configuración completa, tipada y validada."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Catálogo DuckLake (Postgres) ---
    catalog_host: str = "localhost"
    catalog_port: int = Field(default=5433, ge=1, le=65535)
    catalog_user: str = "ducklake"
    catalog_password: SecretStr = SecretStr("ducklake")
    catalog_db: str = "ducklake_catalog"

    # --- Garage S3: esquema, host y puerto por separado (sin host:port) ---
    garage_scheme: Literal["http", "https"] = "http"
    garage_host: str = "localhost"
    garage_s3_port: int = Field(default=3900, ge=1, le=65535)
    garage_s3_key_id: str = _DEFAULT_GARAGE_KEY_ID
    garage_s3_secret: SecretStr = SecretStr(_DEFAULT_GARAGE_SECRET)
    garage_s3_region: str = "garage"

    # --- Lago: bucket y prefijo por separado (la ruta S3 es derivada) ---
    lakehouse_bucket: str = "lakehouse"
    ducklake_data_prefix: str = "data"

    # --- Base de credenciales: explícita, ya no hereda del catálogo ---
    auth_db_host: str = "localhost"
    auth_db_port: int = Field(default=5433, ge=1, le=65535)
    auth_db_user: str = "ducklake"
    auth_db_password: SecretStr = SecretStr("ducklake")
    auth_db_name: str = "kavos_lake_auth"

    # --- JWT y credenciales de usuarios ---
    auth_jwt_secret: SecretStr = SecretStr("")
    auth_jwt_ttl_minutes: int = Field(default=60, ge=1)
    auth_jwt_issuer: str = "kavos-lake-api"
    auth_api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Legado: "usuario:secreto,usuario2:secreto". Sólo se usa para migrar a la
    # base una vez; después puede quedar vacío.
    auth_users: str = ""

    # --- Rate-limiting de login ---
    auth_login_max_attempts: int = Field(default=5, ge=1)
    auth_login_window_seconds: int = Field(default=300, ge=1)
    auth_login_lockout_seconds: int = Field(default=300, ge=1)

    # --- CORS y panel (esquema, hosts y puerto por separado) ---
    cors_scheme: Literal["http", "https"] = "http"
    cors_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1"]
    )
    cors_port: str = "8000"
    cors_explicit_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="cors_allow_origins"
    )
    cors_allow_credentials: bool = False
    session_cookie_secure: bool = False
    # URL pública del panel para armar los links de activación detrás de un
    # proxy. Vacío = se usa el host de la request.
    panel_public_url: str = ""

    # --- Validadores ---

    @field_validator(
        "cors_explicit_origins", "cors_hosts", "auth_api_keys", mode="before"
    )
    @classmethod
    def _parse_csv(cls, value: Any) -> list[str]:
        return _split_csv(value)

    @field_validator("ducklake_data_prefix")
    @classmethod
    def _strip_prefix(cls, value: str) -> str:
        return value.strip().strip("/")

    # --- Valores derivados (nunca entradas editables) ---

    @property
    def cors_allow_origins(self) -> list[str]:
        """Orígenes CORS derivados de esquema, hosts y puerto (o explícitos si se pasaron)."""
        if self.cors_explicit_origins:
            return self.cors_explicit_origins
        ports = _split_csv(self.cors_port) if self.cors_port else [None]
        origins: list[str] = []
        for host in self.cors_hosts:
            if host == "*":
                origins.append("*")
                continue
            if host.startswith("http://") or host.startswith("https://"):
                origins.append(host)
                continue
            for port in ports:
                if (
                    not port
                    or (self.cors_scheme == "http" and port == "80")
                    or (self.cors_scheme == "https" and port == "443")
                ):
                    origins.append(f"{self.cors_scheme}://{host}")
                else:
                    origins.append(f"{self.cors_scheme}://{host}:{port}")
        return origins

    @property
    def garage_s3_endpoint(self) -> str:
        """`host:puerto` para el CREATE SECRET de DuckDB."""
        return f"{self.garage_host}:{self.garage_s3_port}"

    @property
    def garage_use_ssl(self) -> bool:
        return self.garage_scheme == "https"

    @property
    def ducklake_data_path(self) -> str:
        """`s3://bucket/prefijo/`, compuesto desde bucket y prefijo."""
        if not self.ducklake_data_prefix:
            return f"s3://{self.lakehouse_bucket}/"
        return f"s3://{self.lakehouse_bucket}/{self.ducklake_data_prefix}/"

    @property
    def catalog_dsn(self) -> str:
        """DSN libpq del catálogo DuckLake (contiene el secreto: no loguear)."""
        return (
            f"dbname={self.catalog_db} "
            f"host={self.catalog_host} "
            f"port={self.catalog_port} "
            f"user={self.catalog_user} "
            f"password={self.catalog_password.get_secret_value()}"
        )

    @property
    def auth_db_dsn(self) -> str:
        """DSN libpq de la base de credenciales (contiene el secreto: no loguear)."""
        return (
            f"host={self.auth_db_host} "
            f"port={self.auth_db_port} "
            f"user={self.auth_db_user} "
            f"password={self.auth_db_password.get_secret_value()} "
            f"dbname={self.auth_db_name}"
        )


def get_settings() -> Settings:
    """Configuración validada, leída del entorno y del `.env` en cada llamada."""
    return Settings()


def is_internal_host(host: str | None) -> bool:
    """True si el host es local, de red privada o un nombre de servicio Docker.

    Un nombre de un solo label (`postgres-catalog`, `garage`) es interno por
    definición; una IP privada/loopback/link-local también. Cualquier otra cosa
    (dominio o IP pública) se considera expuesta.
    """
    value = (host or "").strip().lower()
    if not value or value in LOOPBACK_HOSTS:
        return True
    if value.endswith(".local") or value.endswith(".internal"):
        return True
    if "." not in value:  # nombre de servicio Docker / hostname corto
        return True
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False  # dominio con punto: asumir público
    return ip.is_private or ip.is_loopback or ip.is_link_local


def assert_safe_credentials(*, host: str | None, secret: str | None, label: str) -> None:
    """Falla si una credencial de ejemplo apunta a un host no interno.

    No rompe local/Docker (ahí el default es aceptable), pero evita desplegar
    con `ducklake`/`password` contra un host público.
    """
    if is_internal_host(host):
        return
    if (secret or "").strip().lower() in DEFAULT_INSECURE_SECRETS:
        raise RuntimeError(
            f"{label}: credencial por defecto o vacía contra el host no interno "
            f"{host!r}. Definí una password explícita antes de exponer el servicio."
        )
