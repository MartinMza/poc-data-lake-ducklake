"""El asistente de entorno no debe perder claves del `.env`.

`generate_merged_env` construye el `.env` nuevo recorriendo las líneas de
`.env.example`. Antes, cualquier clave presente en el `.env` pero ausente en la
plantilla **desaparecía sin aviso**. Eso incluye credenciales ya cargadas y
cualquier variable nueva que todavía no llegó al ejemplo.

El caso que lo destapó: el asistente pasó a preguntar por `ACQUIRE_SINCE_DAYS` y
`ACQUIRE_KEEP_BACKUPS`, pero al no estar en la plantilla las descartaba. Un
asistente que pregunta y después tira la respuesta es peor que uno que no
pregunta.

Los tests usan plantillas sintéticas en `tmp_path`, nunca el `.env` real.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import configure_env as ce  # noqa: E402

PLANTILLA = """# comentario de cabecera
ALPHA=uno
# otro comentario
BETA=dos
GAMMA=tres
"""


def _claves(texto: str) -> dict[str, str]:
    return {
        linea.split("=", 1)[0].strip(): linea.split("=", 1)[1]
        for linea in texto.splitlines()
        if linea.strip() and not linea.strip().startswith("#") and "=" in linea
    }


def _merge(tmp_path: Path, valores: dict[str, str]) -> str:
    plantilla = tmp_path / ".env.example"
    plantilla.write_text(PLANTILLA, encoding="utf-8")
    return ce.generate_merged_env(plantilla, valores)


def test_respeta_el_orden_y_los_comentarios_de_la_plantilla(tmp_path):
    salida = _merge(tmp_path, {"ALPHA": "A", "BETA": "B", "GAMMA": "C"})

    assert "# comentario de cabecera" in salida
    assert "# otro comentario" in salida
    assert salida.index("ALPHA=A") < salida.index("BETA=B") < salida.index("GAMMA=C")


def test_una_clave_fuera_de_la_plantilla_se_conserva(tmp_path):
    # El bug: esta clave se descartaba en silencio.
    salida = _merge(
        tmp_path, {"ALPHA": "A", "BETA": "B", "GAMMA": "C", "ACQUIRE_SINCE_DAYS": "3"}
    )

    assert _claves(salida)["ACQUIRE_SINCE_DAYS"] == "3"
    assert "ausentes en .env.example" in salida


def test_las_claves_extra_van_al_final_con_su_encabezado(tmp_path):
    salida = _merge(tmp_path, {"ALPHA": "A", "ZETA": "z"})

    assert salida.index("ALPHA=A") < salida.index("ZETA=z")
    encabezado = salida.index("ausentes en .env.example")
    assert encabezado < salida.index("ZETA=z")


def test_una_clave_extra_vacia_no_se_escribe(tmp_path):
    # No queremos basura tipo `ALGO=` al final del archivo.
    salida = _merge(tmp_path, {"ALPHA": "A", "VACIA": "", "ESPACIOS": "   "})

    claves = _claves(salida)
    assert "VACIA" not in claves
    assert "ESPACIOS" not in claves


def test_las_credenciales_de_adquisicion_sobreviven_a_un_merge(tmp_path):
    valores = {
        "ACQUIRE_IMAP_USER": "mail.drive.mm@gmail.com",
        "ACQUIRE_IMAP_APP_PASSWORD": "x" * 16,
        "ACQUIRE_LINK_HOSTS": "bucket.s3.us-east-1.amazonaws.com",
        "ACQUIRE_KEEP_BACKUPS": "1",
    }

    claves = _claves(_merge(tmp_path, valores))

    assert claves["ACQUIRE_IMAP_USER"] == valores["ACQUIRE_IMAP_USER"]
    assert claves["ACQUIRE_IMAP_APP_PASSWORD"] == "x" * 16
    assert claves["ACQUIRE_LINK_HOSTS"] == valores["ACQUIRE_LINK_HOSTS"]
    assert claves["ACQUIRE_KEEP_BACKUPS"] == "1"


# --- valores con espacios: el .env generado tiene que ser "sourceable" --------
#
# Defecto real: ACQUIRE_SUBJECT_PATTERN=Backup de Base de Datos Disponible se
# escribía sin comillas, así que `set -a; source .env` dejaba la variable en
# "Backup" y trataba el resto como comandos. Nadie lo notaba porque el filtro
# IMAP matchea por subcadena.

PLANTILLA_ADQUISICION = """# comentario de cabecera
ACQUIRE_SUBJECT_PATTERN=
ACQUIRE_IMAP_USER=
"""


def _generar(tmp_path, valores):
    from configure_env import generate_merged_env

    plantilla = tmp_path / ".env.example"
    plantilla.write_text(PLANTILLA_ADQUISICION, encoding="utf-8")
    return generate_merged_env(plantilla, valores)


def test_un_valor_con_espacios_sobrevive_a_source(tmp_path):
    asunto = "Backup de Base de Datos Disponible"
    contenido = _generar(tmp_path, {"ACQUIRE_SUBJECT_PATTERN": asunto})

    destino = tmp_path / ".env"
    destino.write_text(contenido, encoding="utf-8")
    resultado = subprocess.run(
        ["bash", "-c", f'set -a; source "{destino}"; printf "%s" "$ACQUIRE_SUBJECT_PATTERN"'],
        capture_output=True, text=True, check=False,
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout == asunto
    assert "orden no encontrada" not in resultado.stderr


def test_un_valor_simple_se_escribe_sin_comillas(tmp_path):
    contenido = _generar(tmp_path, {"ACQUIRE_IMAP_USER": "mail.drive.mm@gmail.com"})

    assert "ACQUIRE_IMAP_USER=mail.drive.mm@gmail.com" in contenido


def test_un_valor_con_comilla_simple_cae_a_comillas_dobles(tmp_path):
    contenido = _generar(tmp_path, {"ACQUIRE_SUBJECT_PATTERN": "Backup d'Base"})

    assert 'ACQUIRE_SUBJECT_PATTERN="Backup d\'Base"' in contenido


def test_un_valor_con_dolar_no_se_expande_al_hacer_source(tmp_path):
    contenido = _generar(tmp_path, {"ACQUIRE_IMAP_APP_PASSWORD": "abc$HOME(def)"})

    destino = tmp_path / ".env"
    destino.write_text(contenido, encoding="utf-8")
    resultado = subprocess.run(
        ["bash", "-c", f'set -a; source "{destino}"; printf "%s" "$ACQUIRE_IMAP_APP_PASSWORD"'],
        capture_output=True, text=True, check=False,
    )

    assert resultado.stdout == "abc$HOME(def)"
