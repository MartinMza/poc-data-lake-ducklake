#!/usr/bin/env python3
"""
🌊 KavosLake — Asistente Interactivo de Configuración de Entorno (.env)

Guía paso a paso y automatiza la creación y sincronización del archivo .env
a partir de .env.example, generando secretos criptográficos aleatorios
para cada despliegue (JWT, Garage S3, Postgres y SQL Server).
"""

import argparse
import datetime
import getpass
import os
import re
import secrets
import shutil
import socket
import string
import sys
from pathlib import Path

# --- Colores y Estilos ANSI ---
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text

def bold(text: str) -> str: return _c("1", text)
def cyan(text: str) -> str: return _c("96", text)
def green(text: str) -> str: return _c("92", text)
def yellow(text: str) -> str: return _c("93", text)
def red(text: str) -> str: return _c("91", text)
def blue(text: str) -> str: return _c("94", text)
def dim(text: str) -> str: return _c("2", text)


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT_DIR / ".env"
ENV_EXAMPLE = ROOT_DIR / ".env.example"
MSSQL_BACKUP_DIR = ROOT_DIR / "mssql-backup"

# Secretos dummy o predeterminados del template de ejemplo que no deben usarse en instancias reales
DUMMY_SECRETS = frozenset({
    "GK0123456789abcdef0123456789abcdef",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "ducklake",
    "Str0ng!PoC-Local01",
    "",
})


# --- Generadores de Secretos Criptográficos Aleatorios ---

def generate_jwt_secret() -> str:
    """Genera un secreto HS256 de 32 bytes (64 caracteres hex)."""
    return secrets.token_hex(32)


def generate_garage_key_id() -> str:
    """Genera un Access Key ID con formato Garage: 'GK' + 32 caracteres hex."""
    return "GK" + secrets.token_hex(16)


def generate_garage_secret() -> str:
    """Genera un Secret Key de Garage S3 de 32 bytes (64 caracteres hex)."""
    return secrets.token_hex(32)


def generate_postgres_password(length: int = 24) -> str:
    """Genera una contraseña alfanumérica segura para PostgreSQL."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_mssql_password(length: int = 20) -> str:
    """
    Genera una contraseña aleatoria que cumple estrictamente la política de
    complejidad de SQL Server (mínimo 8 caracteres, al menos 3 categorías de:
    mayúsculas, minúsculas, números y símbolos).
    """
    specials = "!#$*-_"
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(specials),
    ]
    all_chars = string.ascii_letters + string.digits + specials
    remaining = [secrets.choice(all_chars) for _ in range(length - len(required))]
    pw_list = required + remaining
    secrets.SystemRandom().shuffle(pw_list)
    return "".join(pw_list)


def randomize_all_secrets(
    values: dict[str, str],
    force: bool = False,
    preserve_running_db: bool = True,
) -> None:
    """
    Asigna secretos aleatorios únicos a todas las credenciales del stack.
    Si force=False, solo reemplaza aquellos valores que estén vacíos o sean dummies.
    Si preserve_running_db=True y los contenedores de Postgres o SQL Server ya están
    escuchando en sus puertos, conserva sus contraseñas actuales para no invalidar
    los volúmenes ya inicializados en Docker.
    """
    if force or values.get("AUTH_JWT_SECRET") in DUMMY_SECRETS:
        values["AUTH_JWT_SECRET"] = generate_jwt_secret()

    gar_port = 3900
    try:
        gar_port = int(values.get("GARAGE_S3_PORT", 3900))
    except (ValueError, TypeError):
        pass
    gar_running = check_port_in_use(gar_port)

    if gar_running and preserve_running_db and values.get("GARAGE_S3_KEY_ID"):
        if force:
            print(f"  {dim('ℹ️  Garage S3 está activo en puerto ' + str(gar_port) + '; se conservan sus credenciales para no romper el almacenamiento.')}")
    elif force or values.get("GARAGE_S3_KEY_ID") in DUMMY_SECRETS:
        values["GARAGE_S3_KEY_ID"] = generate_garage_key_id()
        values["GARAGE_S3_SECRET"] = generate_garage_secret()

    # Si Postgres ya está corriendo con un volumen creado, cambiar la contraseña
    # en .env causaría "password authentication failed". Conservamos si está activo.
    pg_port = 5433
    try:
        pg_port = int(values.get("CATALOG_PORT", 5433))
    except (ValueError, TypeError):
        pass
    pg_running = check_port_in_use(pg_port)

    if pg_running and preserve_running_db and values.get("CATALOG_PASSWORD"):
        if force:
            print(f"  {dim('ℹ️  PostgreSQL está activo en puerto ' + str(pg_port) + '; se conserva su contraseña para no romper volúmenes existentes.')}")
    elif force or values.get("CATALOG_PASSWORD") in DUMMY_SECRETS:
        pg_pw = generate_postgres_password()
        values["CATALOG_PASSWORD"] = pg_pw
        values["AUTH_DB_PASSWORD"] = pg_pw

    mssql_port = 1433
    try:
        mssql_port = int(values.get("MSSQL_PORT", 1433))
    except (ValueError, TypeError):
        pass
    mssql_running = check_port_in_use(mssql_port)

    if mssql_running and preserve_running_db and values.get("MSSQL_SA_PASSWORD"):
        if force:
            print(f"  {dim('ℹ️  SQL Server está activo en puerto ' + str(mssql_port) + '; se conserva su contraseña para no romper volúmenes existentes.')}")
    elif force or values.get("MSSQL_SA_PASSWORD") in DUMMY_SECRETS:
        values["MSSQL_SA_PASSWORD"] = generate_mssql_password()


def mask_secret(secret: str) -> str:
    """Enmascara un secreto para mostrarlo de forma segura en pantalla."""
    if not secret:
        return dim("(vacío)")
    if len(secret) <= 12:
        return secret[:3] + "..." + secret[-2:]
    return f"{secret[:4]}...{secret[-4:]} ({len(secret)} chars)"


def print_banner():
    banner = f"""
{cyan("╔═══════════════════════════════════════════════════════════════════════════════╗")}
{cyan("║")}   {bold("🌊 KavosLake Data Platform — Asistente de Configuración (.env)")}        {cyan("║")}
{cyan("║")}   {dim("Configura tu entorno con secretos aleatorios únicos y puertos verificados")}  {cyan("║")}
{cyan("╚═══════════════════════════════════════════════════════════════════════════════╝")}
"""
    print(banner)


def check_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Verifica si un puerto TCP ya está escuchando en la máquina."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def parse_env_file(path: Path) -> dict[str, str]:
    """Parsea un archivo .env extrayendo pares KEY -> VALUE."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def generate_merged_env(template_path: Path, current_values: dict[str, str]) -> str:
    """
    Toma la plantilla .env.example y genera el contenido final reemplazando
    cada variable por el valor actual/elegido, conservando comentarios y orden.
    """
    lines: list[str] = []
    with open(template_path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\r\n")
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key, _ = stripped.split("=", 1)
                key = key.strip()
                val = current_values.get(key, "")
                lines.append(f"{key}={val}")
            else:
                lines.append(raw)
    return "\n".join(lines) + "\n"


def create_env_backup() -> Path | None:
    """Crea una copia de respaldo con timestamp si ya existe .env."""
    if not ENV_FILE.is_file():
        return None
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = ROOT_DIR / f".env.bak.{timestamp}"
    shutil.copy2(ENV_FILE, backup_path)
    return backup_path


def maybe_backup_existing_env(force_backup: bool = False, skip_backup: bool = False) -> Path | None:
    """
    Consulta al usuario si desea crear un backup antes de modificar un .env existente.
    Por defecto no se crea a menos que el usuario responda 's' o use el flag --backup.
    """
    if not ENV_FILE.is_file() or skip_backup:
        return None
    if force_backup:
        backup = create_env_backup()
        if backup:
            print(f"  {yellow('✔')} Respaldo del .env previo guardado en: {cyan(backup.name)}")
        return backup
    
    if sys.stdin.isatty():
        resp = prompt_input("¿Deseas crear una copia de respaldo del .env actual antes de modificarlo? (s/N)", "N").upper()
        if resp.startswith("S") or resp.startswith("Y"):
            backup = create_env_backup()
            if backup:
                print(f"  {yellow('✔')} Respaldo del .env guardado en: {cyan(backup.name)}")
            return backup
        else:
            print(f"  {dim('ℹ️  Se omitió la copia de respaldo.')}")
            return None
    return None


def prompt_input(
    prompt: str,
    default: str = "",
    validator: callable = None,
    help_text: str = "",
    secret: bool = False,
) -> str:
    """Solicita un valor al usuario con un default claro y reintentos amigables.

    `secret=True` no ecoa lo tipeado. Se usa para credenciales que no deben
    quedar visibles en la terminal ni en el scrollback de la sesión.
    """
    if help_text:
        print(f"  {dim('ℹ️  ' + help_text)}")

    default_hint = f" [{green(default)}]" if default else ""
    prompt_str = f"  👉 {prompt}{default_hint}: "

    while True:
        try:
            val = (getpass.getpass(prompt_str) if secret else input(prompt_str)).strip()
        except (KeyboardInterrupt, EOFError):
            print("\n" + yellow("Operación cancelada por el usuario."))
            sys.exit(0)
            
        if not val:
            val = default
            
        if validator:
            ok, err_msg = validator(val)
            if not ok:
                print(f"     {red('⚠️  ' + err_msg)}")
                continue
        return val


def validate_port(val: str) -> tuple[bool, str]:
    if not val.isdigit():
        return False, "El puerto debe ser un número entero (ej. 8000)."
    p = int(val)
    if not (1 <= p <= 65535):
        return False, "El puerto debe estar entre 1 y 65535."
    return True, ""


def validate_positive_int(val: str) -> tuple[bool, str]:
    if not val.isdigit() or int(val) < 1:
        return False, "Tiene que ser un entero mayor o igual a 1."
    return True, ""


def validate_mssql_pw(val: str) -> tuple[bool, str]:
    if len(val) < 8:
        return False, "La contraseña debe tener al menos 8 caracteres (requisito de SQL Server)."
    has_upper = any(c.isupper() for c in val)
    has_lower = any(c.islower() for c in val)
    has_digit = any(c.isdigit() for c in val)
    has_sym = any(not c.isalnum() for c in val)
    categories = sum([has_upper, has_lower, has_digit, has_sym])
    if categories < 3:
        return False, "SQL Server exige al menos 3 de: mayúsculas, minúsculas, números y símbolos."
    return True, ""


def get_base_defaults(randomize: bool = True) -> dict[str, str]:
    """Obtiene los valores por defecto desde .env.example con secretos aleatorios únicos."""
    defaults = parse_env_file(ENV_EXAMPLE)
    if randomize:
        randomize_all_secrets(defaults, force=True, preserve_running_db=True)
    return defaults


def run_quick_mode(force_random_secrets: bool = False, force_backup: bool = False, skip_backup: bool = False) -> None:
    """Modo Rápido / 1-Click: Genera configuración recomendada para desarrollo local."""
    print(f"\n{bold('🚀 Generando configuración para PoC / Desarrollo Local...')}")
    
    defaults = get_base_defaults(randomize=True)
    current = parse_env_file(ENV_FILE) if ENV_FILE.is_file() else {}
    
    final_values = defaults.copy()
    
    if current:
        if force_random_secrets:
            # Mantener configuración no secreta pero forzar secretos aleatorios
            for k, v in current.items():
                if k in final_values and v and not (k.endswith("_SECRET") or k.endswith("_KEY_ID") or k.endswith("_PASSWORD")):
                    final_values[k] = v
            randomize_all_secrets(final_values, force=True)
            print(f"  {green('✔')} Nuevos secretos criptográficos generados aleatoriamente.")
        else:
            # Preservar valores existentes y reemplazar los que sean dummy o falten
            for k, v in current.items():
                if k in final_values and v:
                    final_values[k] = v
            # Si contiene secretos dummy de ejemplo, los actualizamos a aleatorios
            randomize_all_secrets(final_values, force=False)
            print(f"  {green('✔')} Valores existentes preservados y secretos validados.")
    else:
        # Nuevo archivo desde cero: todos los secretos generados al azar
        print(f"  {green('✔')} Secretos criptográficos aleatorios generados (JWT, Garage S3, Postgres, MSSQL).")
        
    maybe_backup_existing_env(force_backup=force_backup, skip_backup=skip_backup)
        
    content = generate_merged_env(ENV_EXAMPLE, final_values)
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(content)
        
    MSSQL_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    
    print(f"\n{green('✅ Archivo .env configurado exitosamente!')}")
    show_summary(final_values)


def run_step_by_step_mode(force_backup: bool = False, skip_backup: bool = False) -> None:
    """Asistente interactivo guiado paso a paso con explicaciones claras."""
    print(f"\n{bold('🛠️  Asistente Guiado Paso a Paso')}")
    print(f"{dim('Presiona [Enter] en cada paso para aceptar el valor recomendado (incluye secretos aleatorios).')}\n")
    
    defaults = get_base_defaults(randomize=True)
    current = parse_env_file(ENV_FILE)
    
    def get_initial(key: str, default_random: str = "") -> str:
        curr = current.get(key, "")
        if curr and curr not in DUMMY_SECRETS:
            return curr
        return default_random or defaults.get(key, "")

    chosen = defaults.copy()

    # --- 1. PUERTOS Y ACCESIBILIDAD ---
    print(f"\n{bold(cyan('📌 [1/5] Puertos de los Servicios Locales'))}")
    print("  Define en qué puertos de tu máquina escucharán los servicios.")
    
    # API Port
    def_api = get_initial("API_PORT", "8000")
    in_use = check_port_in_use(int(def_api)) if def_api.isdigit() else False
    hint = f"Puerto para el panel web y API REST. {yellow('⚠️ En uso') if in_use else green('✔ Libre')}"
    api_port = prompt_input("Puerto para API REST y Panel Web", def_api, validate_port, hint)
    chosen["API_PORT"] = api_port
    
    # Prefect Port
    def_pref = get_initial("PREFECT_PORT", "4200")
    in_use = check_port_in_use(int(def_pref)) if def_pref.isdigit() else False
    hint = f"Interfaz gráfica de Prefect para flujos de datos. {yellow('⚠️ En uso') if in_use else green('✔ Libre')}"
    pref_port = prompt_input("Puerto para Prefect UI", def_pref, validate_port, hint)
    chosen["PREFECT_PORT"] = pref_port

    # Catalog Postgres Port
    def_cat = get_initial("CATALOG_PORT", "5433")
    in_use = check_port_in_use(int(def_cat)) if def_cat.isdigit() else False
    hint = f"Puerto Postgres para Catálogo y Auth (5433 evita conflicto con 5432 local). {yellow('⚠️ En uso') if in_use else green('✔ Libre')}"
    cat_port = prompt_input("Puerto para Postgres Catálogo/Auth", def_cat, validate_port, hint)
    chosen["CATALOG_PORT"] = cat_port
    chosen["AUTH_DB_PORT"] = cat_port

    # Garage S3 Port
    def_gar = get_initial("GARAGE_S3_PORT", "3900")
    in_use = check_port_in_use(int(def_gar)) if def_gar.isdigit() else False
    hint = f"Puerto S3 del almacenamiento Garage. {yellow('⚠️ En uso') if in_use else green('✔ Libre')}"
    gar_port = prompt_input("Puerto para Garage S3", def_gar, validate_port, hint)
    chosen["GARAGE_S3_PORT"] = gar_port

    # SQL Server Port
    def_sql = get_initial("MSSQL_PORT", "1433")
    in_use = check_port_in_use(int(def_sql)) if def_sql.isdigit() else False
    hint = f"Puerto de SQL Server donde se restaurará la base .bak. {yellow('⚠️ En uso') if in_use else green('✔ Libre')}"
    sql_port = prompt_input("Puerto para SQL Server", def_sql, validate_port, hint)
    chosen["MSSQL_PORT"] = sql_port

    # --- 2. ALMACENAMIENTO LAGO (DUCKLAKE & S3) Y SECRETOS GARAGE ---
    print(f"\n{bold(cyan('📦 [2/5] Almacenamiento del Lakehouse y Garage S3'))}")
    
    def_bucket = get_initial("LAKEHOUSE_BUCKET", "lakehouse")
    bucket = prompt_input("Nombre del bucket S3 para los archivos Parquet", def_bucket, help_text="Bucket en Garage donde vive el lago.")
    chosen["LAKEHOUSE_BUCKET"] = bucket

    def_prefix = get_initial("DUCKLAKE_DATA_PREFIX", "data")
    prefix = prompt_input("Prefijo de directorio dentro del bucket", def_prefix, help_text="Carpeta virtual (ej. 'data' -> s3://lakehouse/data/).")
    chosen["DUCKLAKE_DATA_PREFIX"] = prefix

    def_gar_key = get_initial("GARAGE_S3_KEY_ID", generate_garage_key_id())
    gar_key = prompt_input("Garage S3 Access Key ID ('GK' + 32 hex)", def_gar_key, help_text="Identificador de clave generado aleatoriamente.")
    chosen["GARAGE_S3_KEY_ID"] = gar_key

    def_gar_sec = get_initial("GARAGE_S3_SECRET", generate_garage_secret())
    gar_sec = prompt_input("Garage S3 Secret Key (64 hex)", def_gar_sec, help_text="Clave secreta S3 generada aleatoriamente.")
    chosen["GARAGE_S3_SECRET"] = gar_sec

    # --- 3. SEGURIDAD Y BASES DE DATOS ---
    print(f"\n{bold(cyan('🔐 [3/5] Seguridad y Contraseñas de Base de Datos'))}")
    
    # Postgres Password
    def_pg_pw = get_initial("CATALOG_PASSWORD", generate_postgres_password())
    hint = "Contraseña para la base de datos PostgreSQL (Catálogo y Auth)."
    pg_pw = prompt_input("Contraseña de Postgres (ducklake)", def_pg_pw, help_text=hint)
    chosen["CATALOG_PASSWORD"] = pg_pw
    chosen["AUTH_DB_PASSWORD"] = pg_pw

    # MSSQL SA Password
    def_sql_pw = get_initial("MSSQL_SA_PASSWORD", generate_mssql_password())
    hint = "Contraseña del administrador 'sa' de SQL Server (generada aleatoriamente cumpliendo complejidad)."
    sql_pw = prompt_input("Contraseña de SQL Server (SA)", def_sql_pw, validate_mssql_pw, hint)
    chosen["MSSQL_SA_PASSWORD"] = sql_pw

    # JWT Secret
    def_jwt = get_initial("AUTH_JWT_SECRET", generate_jwt_secret())
    hint = "Clave HS256 de 64 caracteres hex para firmar los tokens JWT del panel."
    jwt = prompt_input("Clave secreta JWT", def_jwt, help_text=hint)
    chosen["AUTH_JWT_SECRET"] = jwt

    # --- 4. PANEL WEB Y CORS ---
    print(f"\n{bold(cyan('🌐 [4/5] Acceso Web y CORS (Host y Puerto por separado)'))}")
    print(f"  {dim('Regla del PoC: host y puerto van separados; la app compone los orígenes.')}")
    
    def_cors_scheme = get_initial("CORS_SCHEME", "http")
    cors_scheme = prompt_input("Esquema CORS (http o https)", def_cors_scheme, help_text="Protocolo de acceso para el navegador.")
    chosen["CORS_SCHEME"] = cors_scheme

    def_cors_hosts = get_initial("CORS_HOSTS", "localhost,127.0.0.1")
    cors_hosts = prompt_input("Hosts permitidos para CORS (separados por comas o '*')", def_cors_hosts, help_text="Dominios o IPs autorizados para consultar la API.")
    chosen["CORS_HOSTS"] = cors_hosts

    def_cors_port = get_initial("CORS_PORT", chosen["API_PORT"])
    cors_port = prompt_input("Puerto para CORS (o vacío si el host no lleva puerto)", def_cors_port, help_text="Puerto del frontend/panel que consulta la API.")
    chosen["CORS_PORT"] = cors_port

    # --- 5. ADQUISICIÓN AUTOMÁTICA DEL BACKUP ---
    print(f"\n{bold(cyan('📥 [5/5] Adquisición automática del backup (opcional)'))}")
    print(f"  {dim('Con la cuenta vacía la adquisición se salta: seguís cargando el .bak a mano.')}")

    imap_user = prompt_input(
        "Cuenta de Gmail que recibe el aviso (vacío para desactivar)",
        get_initial("ACQUIRE_IMAP_USER", ""),
        help_text="El poller lee este buzón por IMAP. Ej: mail.drive.mm@gmail.com",
    )
    chosen["ACQUIRE_IMAP_USER"] = imap_user

    if imap_user:
        print(f"  {dim('Gmail exige una app password con verificación en dos pasos activa; la')}")
        print(f"  {dim('contraseña normal de la cuenta NO sirve para IMAP.')}")
        chosen["ACQUIRE_IMAP_APP_PASSWORD"] = prompt_input(
            "App password de Gmail (16 caracteres, no se muestra)",
            get_initial("ACQUIRE_IMAP_APP_PASSWORD", ""),
            secret=True,
            help_text="Gmail: Cuenta, Seguridad, Contraseñas de aplicaciones.",
        )

        chosen["ACQUIRE_LINK_HOSTS"] = prompt_input(
            "Allowlist de hosts de descarga (separados por comas)",
            get_initial(
                "ACQUIRE_LINK_HOSTS",
                "finnegans-virginia-db-backup-diario.s3.us-east-1.amazonaws.com",
            ),
            help_text=(
                "Hosts exactos o *.dominio. Un comodín libre habilitaría cualquier "
                "bucket de cualquier cuenta."
            ),
        )

        chosen["ACQUIRE_SINCE_DAYS"] = prompt_input(
            "Días hacia atrás a buscar en el buzón",
            get_initial("ACQUIRE_SINCE_DAYS", "3"),
            validate_positive_int,
            help_text=(
                "Menor que la validez de la firma del link (12 h): más atrás son "
                "correos que ya no se pueden descargar."
            ),
        )

        chosen["ACQUIRE_KEEP_BACKUPS"] = prompt_input(
            "Cuántos backups conservar en mssql-backup/",
            get_initial("ACQUIRE_KEEP_BACKUPS", "1"),
            validate_positive_int,
            help_text="1 = un solo archivo: el nuevo reemplaza al anterior tras verificar checksum.",
        )
    else:
        print(f"  {green('✔')} Adquisición desactivada: carga manual con MSSQL_BAK_PATH.")

    # Confirmación final
    api_url = f"http://localhost:{chosen['API_PORT']}"
    pref_url = f"http://localhost:{chosen['PREFECT_PORT']}"
    lake_path = f"s3://{chosen['LAKEHOUSE_BUCKET']}/{chosen['DUCKLAKE_DATA_PREFIX']}/"
    cors_preview = f"{cors_scheme}://{cors_hosts.split(',')[0]}:{cors_port}" if cors_port and cors_hosts != "*" else f"{cors_scheme}://{cors_hosts}"
    port_desc = cors_port if cors_port else "sin puerto"
    cors_detail = f"(hosts: {cors_hosts}, puerto: {port_desc})"

    print(f"\n{bold('📝 Resumen de la configuración seleccionada:')}")
    print(f"  • API / Panel Web : {cyan(api_url)}")
    print(f"  • Prefect UI      : {cyan(pref_url)}")
    print(f"  • Postgres Port   : {chosen['CATALOG_PORT']}")
    print(f"  • Garage S3 Port  : {chosen['GARAGE_S3_PORT']}")
    print(f"  • SQL Server Port : {chosen['MSSQL_PORT']}")
    print(f"  • Bucket Lago     : {lake_path}")
    print(f"  • Garage Key ID   : {mask_secret(chosen['GARAGE_S3_KEY_ID'])}")
    print(f"  • Postgres PW     : {mask_secret(chosen['CATALOG_PASSWORD'])}")
    print(f"  • SQL Server PW   : {mask_secret(chosen['MSSQL_SA_PASSWORD'])}")
    print(f"  • JWT Secret      : {mask_secret(chosen['AUTH_JWT_SECRET'])}")
    print(f"  • CORS Orígenes   : {cyan(cors_preview)} {dim(cors_detail)}")
    if chosen.get("ACQUIRE_IMAP_USER"):
        cuenta = chosen["ACQUIRE_IMAP_USER"]
        cada = chosen.get("ACQUIRE_POLL_HOURS", "4")
        cuantos = chosen.get("ACQUIRE_KEEP_BACKUPS", "1")
        detalle = f"cada {cada} h, deja {cuantos} backup(s)"
        print(f"  • Adquisición      : {cyan('activada')} {dim(cuenta + ' · ' + detalle)}")
    else:
        print(f"  • Adquisición      : {dim('desactivada (carga manual del .bak)')}")
    
    conf = prompt_input("\n¿Deseas guardar esta configuración en .env? (S/n)", "S").upper()
    if not conf.startswith("S"):
        print(yellow("Configuración cancelada. No se modificó .env."))
        return

    maybe_backup_existing_env(force_backup=force_backup, skip_backup=skip_backup)
        
    content = generate_merged_env(ENV_EXAMPLE, chosen)
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write(content)
        
    MSSQL_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    
    print(f"\n{green('✅ Archivo .env guardado con éxito!')}")
    show_summary(chosen)


def run_diagnose_mode() -> None:
    """Verifica el estado actual de .env y recursos del sistema."""
    print(f"\n{bold('🔍 Diagnóstico y Verificación del Entorno')}\n")
    
    if not ENV_FILE.is_file():
        print(f"  {red('❌ No se encontró archivo .env')}")
        print(f"     Ejecuta el asistente en Modo Rápido (opción 1) para crearlo.")
        return

    cur = parse_env_file(ENV_FILE)
    exp = parse_env_file(ENV_EXAMPLE)
    
    print(f"  {green('✔')} Archivo .env detectado.")
    
    # 1. Comprobar variables faltantes respecto a .env.example
    missing = [k for k in exp.keys() if k not in cur]
    if missing:
        print(f"  {yellow('⚠️  Variables faltantes en .env')} ({len(missing)}):")
        for m in missing:
            print(f"     - {m} (ejemplo: {exp.get(m)})")
    else:
        print(f"  {green('✔')} Todas las variables de .env.example están presentes en .env.")

    # 2. Comprobar variables deprecadas / obsoletas
    obsolete = [k for k in cur.keys() if k.startswith("SOURCE_") or k == "SEED_SCALE" or k == "DUCKLAKE_DATA_PATH"]
    if obsolete:
        print(f"  {yellow('⚠️  Variables obsoletas detectadas (ya no se usan):')}")
        for o in obsolete:
            print(f"     - {o}")
    else:
        print(f"  {green('✔')} Sin variables obsoletas detectadas.")

    # 3. Comprobar calidad y aleatoriedad de los secretos
    print(f"\n  {bold('Evaluación de secretos y credenciales:')}")
    jwt = cur.get("AUTH_JWT_SECRET", "")
    if not jwt:
        print(f"    • JWT Secret     : {red('❌ VACÍO (la API fallará al iniciar)')}")
    elif jwt in DUMMY_SECRETS:
        print(f"    • JWT Secret     : {yellow('⚠️  Usa el secreto de ejemplo (0123456789...). Genera uno aleatorio.')}")
    elif len(jwt) < 32:
        print(f"    • JWT Secret     : {yellow('⚠️  Es corto (<32 bytes).')}")
    else:
        print(f"    • JWT Secret     : {green('✔ Criptográfico y único')} ({mask_secret(jwt)})")

    gar_key = cur.get("GARAGE_S3_KEY_ID", "")
    if gar_key in DUMMY_SECRETS:
        print(f"    • Garage Key ID  : {yellow('⚠️  Usa la clave de ejemplo GK0123456789... (fija)')}")
    else:
        print(f"    • Garage Key ID  : {green('✔ Criptográfico y único')} ({mask_secret(gar_key)})")

    gar_sec = cur.get("GARAGE_S3_SECRET", "")
    if gar_sec in DUMMY_SECRETS:
        print(f"    • Garage Secret  : {yellow('⚠️  Usa el secreto de ejemplo 0123456789... (fijo)')}")
    else:
        print(f"    • Garage Secret  : {green('✔ Criptográfico y único')} ({mask_secret(gar_sec)})")

    pg_pw = cur.get("CATALOG_PASSWORD", "")
    if pg_pw in DUMMY_SECRETS:
        print(f"    • Postgres PW    : {yellow('⚠️  Usa la contraseña por defecto (\"ducklake\")')}")
    else:
        print(f"    • Postgres PW    : {green('✔ Contraseña personalizada/aleatoria')} ({mask_secret(pg_pw)})")

    sql_pw = cur.get("MSSQL_SA_PASSWORD", "")
    if sql_pw in DUMMY_SECRETS:
        print(f"    • SQL Server PW  : {yellow('⚠️  Usa la contraseña de ejemplo (\"Str0ng!PoC-Local01\")')}")
    else:
        print(f"    • SQL Server PW  : {green('✔ Contraseña personalizada/aleatoria')} ({mask_secret(sql_pw)})")

    # 4. Comprobar configuración de CORS
    print(f"\n  {bold('Configuración CORS:')}")
    if "CORS_ALLOW_ORIGINS" in cur and ("CORS_HOSTS" not in cur or "CORS_PORT" not in cur):
        print(f"    • {yellow('ℹ️  CORS_ALLOW_ORIGINS detectado')} (se recomienda migrar a variables separadas: CORS_HOSTS y CORS_PORT).")
    else:
        hosts = cur.get("CORS_HOSTS", "localhost,127.0.0.1")
        port = cur.get("CORS_PORT", "8000")
        scheme = cur.get("CORS_SCHEME", "http")
        print(f"    • {green('✔')} CORS granular: {scheme}://{hosts}:{port or 'sin-puerto'}")

    # 5. Comprobar disponibilidad de puertos
    ports_to_check = {
        "API REST / Panel": ("API_PORT", 8000),
        "Prefect Server": ("PREFECT_PORT", 4200),
        "Postgres Catálogo": ("CATALOG_PORT", 5433),
        "Garage S3": ("GARAGE_S3_PORT", 3900),
        "SQL Server": ("MSSQL_PORT", 1433),
    }
    print(f"\n  {bold('Estado de puertos en tu máquina:')}")
    for name, (var_name, default_p) in ports_to_check.items():
        p_val = cur.get(var_name, str(default_p))
        if p_val.isdigit():
            p = int(p_val)
            in_use = check_port_in_use(p)
            status = yellow("⚠️  En uso (servicio corriendo o ocupado)") if in_use else green("✔ Libre")
            print(f"    • {name:18} (puerto {p:5}): {status}")

    # 6. Comprobar carpeta de backups
    print(f"\n  {bold('Directorio de backups SQL Server:')}")
    if MSSQL_BACKUP_DIR.is_dir():
        baks = list(MSSQL_BACKUP_DIR.glob("*.bak")) + list(MSSQL_BACKUP_DIR.glob("*.tar*"))
        if baks:
            print(f"    {green('✔')} Carpeta ./mssql-backup/ encontrada con {len(baks)} archivo(s):")
            for b in baks[:3]:
                size_mb = b.stat().st_size / (1024 * 1024)
                print(f"      - {b.name} ({size_mb:.1f} MB)")
            if len(baks) > 3:
                print(f"      - ... y {len(baks)-3} más")
        else:
            print(f"    {yellow('ℹ️  Carpeta ./mssql-backup/ lista')}, pero aún no contiene archivos .bak o .tar.")
            print(f"       {dim('Copia tu backup .bak a ./mssql-backup/ antes de ejecutar setup.sh')}")
    else:
        print(f"    {yellow('⚠️  No existe la carpeta ./mssql-backup/')} (se creará automáticamente al configurar).")

    # 7. Adquisición automática del backup (spec bak-acquire)
    print(f"\n  {bold('Adquisición automática del backup:')}")
    _report_adquisicion(cur)

    print(f"\n{green('Diagnóstico completado.')}")


def _report_adquisicion(values: dict[str, str]) -> None:
    """Informa si la adquisición automática quedó operativa.

    Sólo imprime el largo de la app password, nunca el valor. Un pipeline que
    arranca y no baja nada porque falta la allowlist es indistinguible de uno
    que funciona: por eso el estado se dice en voz alta.
    """
    cuenta = values.get("ACQUIRE_IMAP_USER", "")
    app_pw = values.get("ACQUIRE_IMAP_APP_PASSWORD", "")
    hosts = values.get("ACQUIRE_LINK_HOSTS", "")
    if not cuenta and not hosts:
        print(f"  {dim('Adquisición: desactivada (carga manual con MSSQL_BAK_PATH).')}")
        return
    if not cuenta or not hosts or not app_pw:
        faltan = [
            nombre
            for nombre, valor in (
                ("ACQUIRE_IMAP_USER", cuenta),
                ("ACQUIRE_IMAP_APP_PASSWORD", app_pw),
                ("ACQUIRE_LINK_HOSTS", hosts),
            )
            if not valor
        ]
        print(
            f"  {yellow('⚠️  Adquisición incompleta')}: falta "
            f"{', '.join(faltan)}. La tarea se salta en éxito y no baja nada."
        )
        return
    print(
        f"  {green('✔')} Adquisición activa: {cuenta} "
        f"(app password de {len(app_pw.replace(' ', ''))} caracteres), "
        f"deja {values.get('ACQUIRE_KEEP_BACKUPS', '1')} backup(s)."
    )


def show_summary(values: dict[str, str]) -> None:
    api_port = values.get("API_PORT", "8000")
    prefect_port = values.get("PREFECT_PORT", "4200")
    
    print(f"""
{cyan("╔═══════════════════════════════════════════════════════════════════════════════╗")}
{cyan("║")}                       {bold("🚀 ACCESOS DIRECTOS AL STACK")}                       {cyan("║")}
{cyan("╠═══════════════════════════════════════════════════════════════════════════════╣")}
{cyan("║")}  • Panel de Administración : {bold(f"http://localhost:{api_port}/ui"):38} {cyan("║")}
{cyan("║")}  • Documentación OpenAPI   : {bold(f"http://localhost:{api_port}/docs"):38} {cyan("║")}
{cyan("║")}  • Orquestador Prefect     : {bold(f"http://localhost:{prefect_port}"):38} {cyan("║")}
{cyan("╠═══════════════════════════════════════════════════════════════════════════════╣")}
{cyan("║")}                     {bold("🔐 RESUMEN DE SECRETOS ASIGNADOS")}                  {cyan("║")}
{cyan("╠═══════════════════════════════════════════════════════════════════════════════╣")}
{cyan("║")}  • JWT Secret      : {mask_secret(values.get("AUTH_JWT_SECRET", "")):45} {cyan("║")}
{cyan("║")}  • Garage Key ID   : {mask_secret(values.get("GARAGE_S3_KEY_ID", "")):45} {cyan("║")}
{cyan("║")}  • Postgres Pass   : {mask_secret(values.get("CATALOG_PASSWORD", "")):45} {cyan("║")}
{cyan("║")}  • SQL Server Pass : {mask_secret(values.get("MSSQL_SA_PASSWORD", "")):45} {cyan("║")}
{cyan("╠═══════════════════════════════════════════════════════════════════════════════╣")}
{cyan("║")}                        {bold("👉 SIGUIENTES PASOS")}                           {cyan("║")}
{cyan("╠═══════════════════════════════════════════════════════════════════════════════╣")}
{cyan("║")}  1. Coloca tu archivo .bak (o .tar) en la carpeta: {bold("./mssql-backup/"):18} {cyan("║")}
{cyan("║")}  2. Ejecuta el setup para levantar todo el stack:                         {cyan("║")}
{cyan("║")}     {green("./scripts/setup.sh"):56} {cyan("║")}
{cyan("║")}  3. Al terminar, entra al panel {cyan(f"http://localhost:{api_port}/ui")} con el            {cyan("║")}
{cyan("║")}     SETUP TOKEN impreso en consola para crear tu superAdmin.                {cyan("║")}
{cyan("╚═══════════════════════════════════════════════════════════════════════════════╝")}
""")


    _report_adquisicion(values)


def main():
    parser = argparse.ArgumentParser(
        description="🌊 KavosLake — Asistente interactivo de configuración de entorno (.env)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--quick", "--auto", action="store_true", help="Configura automáticamente .env con valores recomendados.")
    parser.add_argument("--random-secrets", "--fresh-secrets", action="store_true", help="Fuerza la generación de nuevos secretos aleatorios únicos.")
    parser.add_argument("--backup", action="store_true", help="Crea copia de respaldo (.env.bak) antes de modificar.")
    parser.add_argument("--no-backup", action="store_true", help="Omite la copia de respaldo sin preguntar.")
    parser.add_argument("--check", "--diagnose", action="store_true", help="Diagnostica el estado actual de .env y puertos.")
    parser.add_argument("--step-by-step", action="store_true", help="Inicia directamente el asistente paso a paso.")
    args = parser.parse_args()

    force_b = args.backup
    skip_b = args.no_backup

    # Si se pasó un flag específico, se ejecuta directamente
    if args.random_secrets:
        print_banner()
        run_quick_mode(force_random_secrets=True, force_backup=force_b, skip_backup=skip_b)
        return
    if args.quick:
        print_banner()
        run_quick_mode(force_backup=force_b, skip_backup=skip_b)
        return
    if args.check:
        print_banner()
        run_diagnose_mode()
        return
    if args.step_by_step:
        print_banner()
        run_step_by_step_mode(force_backup=force_b, skip_backup=skip_b)
        return

    # Si no hay flags, mostramos el menú principal interactivo
    print_banner()
    
    print(f"¿Qué deseas hacer hoy?\n")
    print(f"  {bold('1)')} 🚀 {bold('Modo Rápido')} {dim('(Crea o actualiza .env con puertos estándar y secretos validados)')}")
    print(f"  {bold('2)')} 🛠️  {bold('Modo Paso a Paso')} {dim('(Asistente guiado donde puedes revisar y confirmar cada secreto)')}")
    print(f"  {bold('3)')} 🔐 {bold('Regenerar Secretos Aleatorios')} {dim('(Genera nuevas claves aleatorias para JWT, Garage, Postgres y MSSQL)')}")
    print(f"  {bold('4)')} 🔍 {bold('Diagnosticar y verificar')} {dim('(Verifica si usas claves de ejemplo o si hay puertos ocupados)')}")
    print(f"  {bold('5)')} 🚪 Salir\n")

    choice = prompt_input("Selecciona una opción [1-5]", "1")
    
    if choice == "1":
        run_quick_mode()
    elif choice == "2":
        run_step_by_step_mode()
    elif choice == "3":
        run_quick_mode(force_random_secrets=True)
    elif choice == "4":
        run_diagnose_mode()
    else:
        print("Hasta luego.")


if __name__ == "__main__":
    main()
