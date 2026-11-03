"""T2 / Spec S5: estado persistido entre corridas.

El estado recuerda qué mensajes ya se procesaron y qué backup fue el último
aplicado con éxito. Dos propiedades se prueban acá porque son las que pueden
morder:

- **Nunca guarda la URL.** La URL presignada es una credencial STS viva; si
  cae en el estado, cae en el disco y en el próximo commit.
- **Archivo corrupto falla cerrado.** Un estado ilegible no puede degradar a
  "sin backup aplicado", porque eso desactivaría la guarda de S3 y dejaría
  pasar un artefacto viejo.
"""

from __future__ import annotations

import json

import pytest

from kavos_acquire.identity import BackupId, parse_backup_id
from kavos_acquire.state import (
    MAX_PROCESSED_MESSAGE_IDS,
    AcquireState,
    StateError,
    load_state,
    save_state,
)

S3_ARTIFACT = "FAF12_Kavos_2026_11_01_233302_905705.bak.tar.gz"
MESSAGE_ID = "<CAP.abc123@mail.gmail.com>"


def test_archivo_ausente_es_estado_vacio(tmp_path):
    estado = load_state(tmp_path / "no-existe.json")

    assert estado.last_uid == 0
    assert estado.processed_message_ids == []
    assert estado.applied is None


def test_archivo_corrupto_falla_cerrado(tmp_path):
    ruta = tmp_path / "state.json"
    ruta.write_text("{esto no es json", encoding="utf-8")

    with pytest.raises(StateError) as exc:
        load_state(ruta)

    assert "state.json" in str(exc.value)


def test_roundtrip_save_load(tmp_path):
    ruta = tmp_path / "state.json"
    estado = AcquireState(last_uid=42)
    estado.note_message(MESSAGE_ID)
    estado.note_applied(parse_backup_id(S3_ARTIFACT))

    save_state(ruta, estado)
    recuperado = load_state(ruta)

    assert recuperado.last_uid == 42
    assert recuperado.processed_message_ids == [MESSAGE_ID]
    assert recuperado.applied == parse_backup_id(S3_ARTIFACT)


def test_save_no_deja_archivo_temporal(tmp_path):
    ruta = tmp_path / "state.json"

    save_state(ruta, AcquireState(last_uid=7))

    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
    assert json.loads(ruta.read_text(encoding="utf-8"))["last_uid"] == 7


def test_un_message_id_repetido_no_duplica(tmp_path):
    ruta = tmp_path / "state.json"
    estado = AcquireState()
    estado.note_message(MESSAGE_ID)
    estado.note_message(MESSAGE_ID)

    save_state(ruta, estado)

    assert load_state(ruta).processed_message_ids == [MESSAGE_ID]


def test_la_lista_de_mensajes_se_acota(tmp_path):
    ruta = tmp_path / "state.json"
    estado = AcquireState()
    for n in range(MAX_PROCESSED_MESSAGE_IDS + 1):
        estado.note_message(f"<m{n}@mail.gmail.com>")

    save_state(ruta, estado)
    recuperado = load_state(ruta)

    assert len(recuperado.processed_message_ids) == MAX_PROCESSED_MESSAGE_IDS
    # Se descarta el más viejo, se conserva el más nuevo.
    assert "<m0@mail.gmail.com>" not in recuperado.processed_message_ids
    assert f"<m{MAX_PROCESSED_MESSAGE_IDS}@mail.gmail.com>" in recuperado.processed_message_ids


def test_el_uid_no_retrocede():
    estado = AcquireState(last_uid=5)
    estado.note_uid(3)

    assert estado.last_uid == 5


def test_el_estado_persistido_no_contiene_urls(tmp_path):
    # Spec S5: la capacidad de descarga nunca toca el disco.
    ruta = tmp_path / "state.json"
    estado = AcquireState(last_uid=9)
    estado.note_message(MESSAGE_ID)
    estado.note_applied(parse_backup_id(S3_ARTIFACT))

    save_state(ruta, estado)
    crudo = ruta.read_text(encoding="utf-8")

    for prohibido in ("http", "X-Amz", "Signature", "Security-Token"):
        assert prohibido not in crudo


def test_applied_sobrevive_como_objeto_comparable(tmp_path):
    ruta = tmp_path / "state.json"
    estado = AcquireState()
    estado.note_applied(parse_backup_id(S3_ARTIFACT))

    save_state(ruta, estado)
    recuperado = load_state(ruta)

    assert isinstance(recuperado.applied, BackupId)
    assert recuperado.applied.stamp.year == 2026
