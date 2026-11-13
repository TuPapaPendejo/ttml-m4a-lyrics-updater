#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Actualiza exclusivamente el átomo ©lyr de archivos M4A usando TTML.
Versión 3.7: selector interactivo de carpeta de origen; lotes secuenciales.

- Convierte begin de TTML a marcas LRC [MM:SS.cc].
- Inserta itunes:songPart como una línea LRC sincronizada antes de cada sección.
- Representa los espacios entre bloques como líneas LRC vacías sincronizadas,
  para que OpenLyrics no las reordene al final.
- Sincroniza el bloque de créditos al final de la canción para que OpenLyrics
  también lo muestre en su panel de letras sincronizadas.
- Acepta reloj TTML (MM:SS.mmm / HH:MM:SS.mmm), segundos decimales
  sin unidad (por ejemplo 7.627) y offsets con unidades h, m, s y ms.
- No decodifica ni vuelve a codificar el audio.
- Trabaja sobre una copia temporal y sólo reemplaza el original tras validar:
  * hash idéntico del payload de todos los átomos MP4 mdat;
  * todos los demás tags Mutagen idénticos;
  * ©lyr exactamente igual al LRC generado.
- No crea copias de seguridad persistentes.
- Procesa archivos secuencialmente en lotes configurables; nunca ejecuta escrituras
  concurrentes. Entre lotes aplica una pausa breve configurable.
- Registra los errores con ruta absoluta del M4A y del TTML, mensaje y traceback
  en un archivo LOG dentro de la carpeta raíz (o en la ruta indicada).

El modo predeterminado es simulación. Para escribir, use --apply.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import os
import re
import shutil
import struct
import sys
import tempfile
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

try:
    from mutagen.mp4 import MP4
except ImportError:
    print("ERROR: Falta Mutagen. Instálelo con: python -m pip install mutagen==1.48.1", file=sys.stderr)
    raise

LYRICS_KEY = "\xa9lyr"
BACKUP_PREFIX = "_TTML_LYRICS_BACKUP_"
NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
OFFSET_RE = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(ms|h|m|s)$", re.IGNORECASE)


def local_name(tag: str) -> str:
    """Devuelve el nombre local de una etiqueta XML, ignorando el namespace."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag.split(":")[-1]


def parse_ttml_time(value: str) -> Decimal:
    """Convierte una expresión de tiempo TTML a segundos decimales."""
    value = value.strip()
    if not value:
        raise ValueError("atributo begin vacío")

    # TTML offset-time con unidad explícita.
    match = OFFSET_RE.fullmatch(value)
    if match:
        try:
            amount = Decimal(match.group(1))
        except InvalidOperation as exc:
            raise ValueError(f"tiempo no válido: {value!r}") from exc
        unit = match.group(2).lower()
        factor = {"h": Decimal(3600), "m": Decimal(60), "s": Decimal(1), "ms": Decimal("0.001")}[unit]
        seconds = amount * factor
    elif ":" in value:
        parts = value.split(":")
        try:
            if len(parts) == 2:
                # mm:ss[.fraction]
                minutes = Decimal(parts[0])
                secs = Decimal(parts[1])
                seconds = minutes * 60 + secs
            elif len(parts) == 3:
                # hh:mm:ss[.fraction]
                hours = Decimal(parts[0])
                minutes = Decimal(parts[1])
                secs = Decimal(parts[2])
                seconds = hours * 3600 + minutes * 60 + secs
            else:
                raise ValueError
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Formato de tiempo no válido: {value!r}") from exc
    elif NUMBER_RE.fullmatch(value):
        # En los TTML de Apple también aparece begin="7.627"; se interpreta
        # como 7.627 segundos, no como un timestamp MM:SS.
        try:
            seconds = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"Tiempo no válido: {value!r}") from exc
    else:
        raise ValueError(
            f"Formato de tiempo no soportado: {value!r}. Se admiten segundos decimales, "
            "MM:SS.fraction, HH:MM:SS.fraction y unidades h/m/s/ms."
        )

    if seconds < 0:
        raise ValueError(f"No se admiten tiempos negativos: {value!r}")
    return seconds


def format_lrc_timestamp(seconds: Decimal) -> str:
    """Devuelve [MM:SS.cc], redondeando centésimas con ROUND_HALF_UP."""
    centiseconds = int((seconds * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    minutes, remainder = divmod(centiseconds, 6000)
    whole_seconds, cs = divmod(remainder, 100)
    return f"[{minutes:02d}:{whole_seconds:02d}.{cs:02d}]"


def collect_text(element: ET.Element) -> str:
    """Extrae texto de un p, conservando spans y sustituyendo br por espacio."""
    pieces: list[str] = []

    def walk(node: ET.Element) -> None:
        if node.text:
            pieces.append(node.text)
        for child in list(node):
            if local_name(child.tag).lower() in {"br", "break"}:
                pieces.append(" ")
            else:
                walk(child)
            if child.tail:
                pieces.append(child.tail)

    walk(element)
    # Elimina saltos/indentación del XML sin alterar puntuación ni espacios
    # internos significativos entre palabras.
    return re.sub(r"\s+", " ", "".join(pieces)).strip()


def get_attr_by_local(element: ET.Element, wanted: str) -> str | None:
    """Obtiene un atributo XML por su nombre local, ignorando namespaces."""
    wanted = wanted.casefold()
    for key, value in element.attrib.items():
        if local_name(key).casefold() == wanted:
            return value
    return None


def clean_metadata_value(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def humanize_metadata_name(name: str) -> str:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    name = name.replace("_", " ").replace("-", " ")
    return name.strip().capitalize()


def metadata_label(name: str) -> str:
    """Mapea nombres habituales de metadatos TTML a etiquetas legibles."""
    key = re.sub(r"[^a-z0-9]", "", name.casefold())
    labels = {
        "songwriter": "Autores (songwriters)",
        "songwriters": "Autores (songwriters)",
        "composer": "Compositores",
        "composers": "Compositores",
        "lyricist": "Letristas",
        "lyricists": "Letristas",
        "producer": "Productores",
        "producers": "Productores",
        "performer": "Intérpretes",
        "performers": "Intérpretes",
        "artist": "Artistas",
        "artists": "Artistas",
        "publisher": "Editoriales",
        "publishers": "Editoriales",
        "arranger": "Arreglistas",
        "arrangers": "Arreglistas",
        "engineer": "Ingeniería de audio",
        "engineers": "Ingeniería de audio",
        "recordingengineer": "Ingeniería de grabación",
        "mixingengineer": "Ingeniería de mezcla",
        "masteringengineer": "Ingeniería de masterización",
        "label": "Sello discográfico",
        "copyright": "Copyright",
        "isrc": "ISRC",
        "translations": "Traducciones",
    }
    return labels.get(key, humanize_metadata_name(name))


def format_credit_time(value: str) -> str:
    """Formatea un tiempo TTML en MM:SS.cc para mostrarlo en créditos."""
    return format_lrc_timestamp(parse_ttml_time(value))[1:-1]


def extract_ttml_credits(root: ET.Element) -> list[str]:
    """Extrae exclusivamente campos no vacíos del elemento iTunesMetadata.

    La extracción es genérica: no depende de una lista fija de nombres de
    campo. Conserva atributos con valor y texto de cualquier descendiente,
    agrupa listas homogéneas (p. ej. varios <songwriter>) y omite nodos vacíos.
    No incorpora xml:lang/itunes:timing del elemento <tt>, datos de <body>
    ni agentes TTML de <head> que estén fuera de iTunesMetadata.
    """
    result: list[str] = []
    seen: set[str] = set()

    def nonempty_attributes(element: ET.Element) -> list[tuple[str, str]]:
        values: list[tuple[str, str]] = []
        for raw_name, raw_value in element.attrib.items():
            value = clean_metadata_value(raw_value)
            if value:
                values.append((local_name(raw_name), value))
        return values

    def attrs_suffix(attributes: list[tuple[str, str]]) -> str:
        if not attributes:
            return ""
        return " (" + "; ".join(f"{name}={value}" for name, value in attributes) + ")"

    def has_content(element: ET.Element) -> bool:
        if clean_metadata_value(element.text) or nonempty_attributes(element):
            return True
        return any(has_content(child) for child in list(element))

    def render_element(element: ET.Element, depth: int = 0) -> list[str]:
        """Renderiza un campo de metadata, omitiendo subárboles vacíos."""
        label = metadata_label(local_name(element.tag))
        own_text = clean_metadata_value(element.text)
        attributes = nonempty_attributes(element)
        children = [child for child in list(element) if has_content(child)]
        indent = "  " * depth
        suffix = attrs_suffix(attributes)

        if not children:
            if own_text:
                return [f"{indent}{label}{suffix}: {own_text}"]
            if attributes:
                return [f"{indent}{label}{suffix}"]
            return []

        lines: list[str] = []
        if own_text:
            lines.append(f"{indent}{label}{suffix}: {own_text}")
        elif attributes:
            lines.append(f"{indent}{label}{suffix}")

        # Caso común: <songwriters><songwriter>...</songwriter>...</songwriters>.
        # Se agrupan valores repetidos sin fijar nombres de campo permitidos.
        child_names = {local_name(child.tag).casefold() for child in children}
        all_simple_text_leaves = all(
            not list(child)
            and bool(clean_metadata_value(child.text))
            and not nonempty_attributes(child)
            for child in children
        )
        if len(child_names) == 1 and all_simple_text_leaves:
            values: list[str] = []
            for child in children:
                value = clean_metadata_value(child.text)
                if value not in values:
                    values.append(value)
            if not own_text and not attributes:
                return [f"{indent}{label}: {'; '.join(values)}"]
            lines.append(f"{indent}  {metadata_label(local_name(children[0].tag))}: {'; '.join(values)}")
            return lines

        # Contenedores con estructuras más complejas conservan la jerarquía.
        if not own_text and not attributes:
            lines.append(f"{indent}{label}:")
        for child in children:
            lines.extend(render_element(child, depth + 1))
        return lines

    for container in root.iter():
        if local_name(container.tag).casefold() != "itunesmetadata":
            continue

        # Los atributos de iTunesMetadata también forman parte de su contenido.
        for attr_name, attr_value in nonempty_attributes(container):
            line = f"{metadata_label(attr_name)}: {attr_value}"
            if line.casefold() not in seen:
                seen.add(line.casefold())
                result.append(line)

        for child in list(container):
            for line in render_element(child):
                key = line.casefold()
                if key not in seen:
                    seen.add(key)
                    result.append(line)

    return result


def iter_lyric_paragraphs(root: ET.Element):
    """Recorre <p> en orden XML e informa su sección y contenedor <div>.

    El identificador del div permite preservar una línea en blanco entre
    bloques TTML, incluso cuando un div no declara itunes:songPart o cuando
    se repite el mismo valor de songPart en dos divs consecutivos.
    """
    def walk(
        node: ET.Element,
        inherited_section: tuple[ET.Element, str] | None,
        current_div: ET.Element | None,
    ):
        if local_name(node.tag).casefold() == "div":
            current_div = node

        song_part = get_attr_by_local(node, "songPart")
        if song_part is not None:
            value = clean_metadata_value(song_part)
            inherited_section = (node, value) if value else None

        if local_name(node.tag).casefold() == "p":
            yield node, inherited_section, current_div
            return

        for child in list(node):
            yield from walk(child, inherited_section, current_div)

    yield from walk(root, None, None)


def get_body_duration(root: ET.Element) -> Decimal | None:
    """Obtiene dur del primer <body>, si existe y es interpretable."""
    for element in root.iter():
        if local_name(element.tag).casefold() == "body":
            value = get_attr_by_local(element, "dur")
            if value:
                try:
                    return parse_ttml_time(value)
                except ValueError:
                    return None
    return None


def get_lrc_credit_times(
    root: ET.Element,
    last_lyric_boundary: Decimal | None,
    credit_line_count: int,
) -> list[Decimal]:
    """Asigna tiempos LRC a los créditos para que OpenLyrics no los descarte.

    OpenLyrics clasifica el texto no sincronizado después de la letra temporizada.
    Se espacian los créditos 1 s y, cuando existe <body dur>, se colocan en el
    tramo final disponible, pero nunca antes del límite de la última línea.
    """
    if credit_line_count <= 0:
        return []

    spacing = Decimal("1.00")
    earliest = (last_lyric_boundary or Decimal(0)) + Decimal("0.05")
    body_duration = get_body_duration(root)
    if body_duration is not None:
        preferred_start = body_duration - spacing * Decimal(credit_line_count)
        start = max(earliest, preferred_start)
    else:
        start = earliest

    return [start + spacing * Decimal(index) for index in range(credit_line_count)]


def ttml_to_lrc(ttml_path: Path, include_credits: bool = True) -> tuple[str, int, list[str]]:
    try:
        raw = ttml_path.read_bytes()
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"No se pudo leer/analizar el TTML: {exc}") from exc

    lines: list[str] = []
    skipped_without_begin = 0
    last_section_element: ET.Element | None = None
    last_div_element: ET.Element | None = None
    have_lyric_block = False
    lyric_begin_times: list[Decimal] = []
    lyric_end_times: list[Decimal] = []

    for element, section, div_element in iter_lyric_paragraphs(root):
        begin = get_attr_by_local(element, "begin")
        if begin is None:
            # Algunas variantes pueden declarar un p sin begin; no inventamos
            # una marca de tiempo ni heredamos tiempos ambiguos del contenedor.
            skipped_without_begin += 1
            continue
        lyric = collect_text(element)
        if not lyric:
            continue

        lyric_seconds = parse_ttml_time(begin)
        lyric_begin_times.append(lyric_seconds)
        end_value = get_attr_by_local(element, "end")
        if end_value:
            try:
                lyric_end_times.append(parse_ttml_time(end_value))
            except ValueError:
                pass

        div_changed = div_element is not last_div_element
        section_changed = section is not None and section[0] is not last_section_element
        block_changed = div_changed or section_changed

        # El encabezado va ligeramente antes del primer p de su bloque. Si el
        # div proporciona begin, se prefiere ese instante cuando es anterior.
        section_marker_seconds = lyric_seconds - Decimal("0.01")
        if section_changed:
            section_begin = get_attr_by_local(section[0], "begin")
            if section_begin:
                try:
                    section_marker_seconds = min(
                        parse_ttml_time(section_begin), section_marker_seconds
                    )
                except ValueError:
                    pass
        section_marker_seconds = max(Decimal(0), section_marker_seconds)

        # IMPORTANTE PARA OPENLYRICS:
        # No se emite una línea vacía sin timestamp. El parser LRC de OpenLyrics
        # ordena todas las líneas sin tiempo al final del archivo. Un timestamp
        # sin texto representa una línea visual vacía, pero permanece en su
        # posición cronológica y el panel la dibuja como espacio entre bloques.
        if have_lyric_block and block_changed:
            gap_anchor = section_marker_seconds if section_changed else lyric_seconds - Decimal("0.01")
            gap_seconds = max(Decimal(0), gap_anchor - Decimal("0.01"))
            gap_timestamp = format_lrc_timestamp(gap_seconds)
            anchor_timestamp = format_lrc_timestamp(gap_anchor)
            if gap_timestamp != anchor_timestamp:
                lines.append(gap_timestamp)  # Línea sincronizada vacía intencional.

        # Inserta itunes:songPart como una línea LRC sincronizada antes de la
        # primera letra de la sección.
        if section_changed:
            marker_timestamp = format_lrc_timestamp(section_marker_seconds)
            lines.append(f"{marker_timestamp}{section[1]}")

        last_section_element = section[0] if section is not None else None
        last_div_element = div_element
        have_lyric_block = True

        timestamp = format_lrc_timestamp(lyric_seconds)
        lines.append(f"{timestamp}{lyric}")

    if not lyric_begin_times or not any(line.startswith("[") for line in lines):
        detail = f"; {skipped_without_begin} elemento(s) p sin begin" if skipped_without_begin else ""
        raise ValueError(f"El TTML no contiene párrafos p con begin y texto{detail}")

    # Se conserva el orden temporal original de los párrafos TTML; esto evita
    # reordenar líneas que se solapan temporalmente.
    credits = extract_ttml_credits(root) if include_credits else []
    if credits:
        credit_block = ["[Créditos y metadatos TTML]", *credits]
        # Para OpenLyrics, cada renglón del bloque final también necesita
        # timestamp: de otro modo se clasifica como texto sin sincronizar y
        # queda separado del resto de la línea de tiempo.
        last_lyric_boundary = max(
            lyric_end_times + lyric_begin_times
        )
        credit_times = get_lrc_credit_times(root, last_lyric_boundary, len(credit_block))
        first_credit_time = credit_times[0]
        separator_time = max(Decimal(0), first_credit_time - Decimal("0.01"))
        if format_lrc_timestamp(separator_time) != format_lrc_timestamp(first_credit_time):
            lines.append(format_lrc_timestamp(separator_time))
        for credit_text, credit_time in zip(credit_block, credit_times):
            lines.append(f"{format_lrc_timestamp(credit_time)}{credit_text}")

    return "\n".join(lines), skipped_without_begin, credits


def freeze_value(value: Any) -> Any:
    """Convierte un valor Mutagen a una representación comparable y estable."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        extra = []
        for attr in ("dataformat", "version", "imageformat"):
            if hasattr(value, attr):
                extra.append((attr, repr(getattr(value, attr))))
        return (type(value).__name__, bytes(value), tuple(extra))
    if isinstance(value, dict):
        return (type(value).__name__, tuple(sorted((str(k), freeze_value(v)) for k, v in value.items())))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, tuple(freeze_value(v) for v in value))
    if value is None or isinstance(value, (str, int, float, bool)):
        return (type(value).__name__, value)
    # Para objetos Mutagen no cubiertos arriba, conserva tipo y repr.
    return (type(value).__name__, repr(value))


def snapshot_non_lyrics_tags(tags: Any) -> tuple:
    if tags is None:
        return tuple()
    return tuple(
        sorted(
            (str(key), freeze_value(value))
            for key, value in tags.items()
            if str(key) != LYRICS_KEY
        )
    )


def get_lyrics_value(tags: Any) -> str | None:
    if tags is None:
        return None
    value = tags.get(LYRICS_KEY)
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else None
    return str(value)


def mdat_payload_sha256(path: Path) -> str:
    """Calcula SHA-256 del payload de todos los átomos mdat de primer nivel."""
    file_size = path.stat().st_size
    digest = hashlib.sha256()
    found = 0
    with path.open("rb") as stream:
        offset = 0
        while offset < file_size:
            stream.seek(offset)
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError(f"Estructura MP4 incompleta cerca del byte {offset}")
            size32, box_type = struct.unpack(">I4s", header)
            header_size = 8
            if size32 == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    raise ValueError(f"Átomo MP4 extendido incompleto cerca del byte {offset}")
                box_size = struct.unpack(">Q", extended)[0]
                header_size = 16
            elif size32 == 0:
                box_size = file_size - offset
            else:
                box_size = size32

            if box_size < header_size or offset + box_size > file_size:
                raise ValueError(f"Tamaño de átomo MP4 inválido cerca del byte {offset}")

            if box_type == b"mdat":
                found += 1
                remaining = box_size - header_size
                stream.seek(offset + header_size)
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Fin inesperado al leer el contenido mdat")
                    digest.update(chunk)
                    remaining -= len(chunk)
            offset += box_size

    if found == 0:
        raise ValueError("No se encontró ningún átomo mdat de primer nivel; se cancela por seguridad")
    return digest.hexdigest()


def find_ttml_for_m4a(m4a_path: Path) -> Path | None:
    # Convenciones compatibles: Pista.ttml y Pista.m4a.ttml.
    candidates = [m4a_path.with_suffix(".ttml"), Path(str(m4a_path) + ".ttml")]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Respaldo case-insensitive por si las mayúsculas de la extensión difieren.
    try:
        names = {entry.name.casefold(): entry for entry in m4a_path.parent.iterdir() if entry.is_file()}
    except OSError:
        raise
    for name in (m4a_path.stem + ".ttml", m4a_path.name + ".ttml"):
        found = names.get(name.casefold())
        if found is not None:
            return found
    return None


def iter_m4a_files(root: Path, onerror=None):
    for current, dirs, files in os.walk(root, onerror=onerror):
        dirs[:] = [d for d in dirs if not d.startswith(BACKUP_PREFIX)]
        for filename in files:
            if filename.lower().endswith(".m4a"):
                yield Path(current) / filename


def relative_display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def write_updated_file(m4a_path: Path, lyrics: str) -> None:
    """Actualiza ©lyr usando un temporal validado, sin crear respaldo persistente.

    El original sólo se sustituye después de verificar el temporal completo.
    El temporal está en la misma carpeta para que os.replace no cruce volúmenes.
    """
    # 1. Captura el estado original; si no podemos validarlo, no tocamos el archivo.
    original_audio = MP4(str(m4a_path))
    before_tags = snapshot_non_lyrics_tags(original_audio.tags)
    before_mdat = mdat_payload_sha256(m4a_path)
    original_lyrics = get_lyrics_value(original_audio.tags)
    if original_lyrics == lyrics:
        return

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".ttml_lyrics_", suffix=".tmp", dir=str(m4a_path.parent), delete=False
        ) as temp_file:
            temp_name = temp_file.name
        temp_path = Path(temp_name)
        shutil.copy2(m4a_path, temp_path)

        edited_audio = MP4(str(temp_path))
        if edited_audio.tags is None:
            edited_audio.add_tags()
        edited_audio.tags[LYRICS_KEY] = [lyrics]
        edited_audio.save()

        # 2. Valida el temporal ANTES de sustituir el original.
        check_audio = MP4(str(temp_path))
        if get_lyrics_value(check_audio.tags) != lyrics:
            raise ValueError("La lectura posterior no coincide con la letra LRC escrita en ©lyr")
        if snapshot_non_lyrics_tags(check_audio.tags) != before_tags:
            raise ValueError("Mutagen detectó cambios en etiquetas distintas de ©lyr; se cancela el reemplazo")
        after_mdat = mdat_payload_sha256(temp_path)
        if after_mdat != before_mdat:
            raise ValueError("El contenido mdat cambió; se cancela el reemplazo para proteger el audio")

        # 3. Sustitución atómica. No se conserva una segunda copia completa.
        # Si os.replace falla, el archivo original permanece en su ubicación.
        os.replace(temp_path, m4a_path)
        temp_name = None
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def create_run_logger(root: Path, requested_path: str | None) -> tuple[logging.Logger, Path]:
    """Crea un log UTF-8; por defecto queda en la raíz de la biblioteca."""
    if requested_path:
        log_path = Path(requested_path).expanduser()
        if not log_path.is_absolute():
            log_path = root / log_path
        log_path = log_path.resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_path = root / f"Actualizar-Letras_{stamp}.log"

    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ttml_lyrics_update")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    return logger, log_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Actualiza sólo ©lyr de M4A a partir de TTML; por defecto simula sin escribir."
    )
    parser.add_argument("--root", required=True, help="Carpeta raíz que contiene la biblioteca M4A/TTML")
    parser.add_argument("--apply", action="store_true", help="Escribe los cambios; sin esta opción sólo simula")
    parser.add_argument("--no-credits", action="store_true", help="No añade créditos/metadatos TTML al final de ©lyr")
    parser.add_argument(
        "--log-file",
        help="Ruta del log. Si se omite, se crea Actualizar-Letras_FECHA_HORA.log en la carpeta raíz.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Número máximo de archivos por lote secuencial (predeterminado: 10).",
    )
    parser.add_argument(
        "--batch-pause-seconds",
        type=float,
        default=1.0,
        help="Pausa entre lotes en segundos; use 0 para desactivarla (predeterminado: 1).",
    )
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size debe ser un entero mayor o igual que 1")
    if not math.isfinite(args.batch_pause_seconds) or args.batch_pause_seconds < 0:
        parser.error("--batch-pause-seconds debe ser un número finito mayor o igual que 0")

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: La carpeta no existe o no es accesible: {root}", file=sys.stderr)
        return 2

    try:
        logger, log_path = create_run_logger(root, args.log_file)
    except OSError as exc:
        print(f"ERROR: No se pudo crear el log en {root}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    mode = "APLICAR CAMBIOS" if args.apply else "SIMULACIÓN (sin escribir)"
    print(f"\nBiblioteca : {root}")
    print(f"Modo       : {mode}")
    print("Formato    : LRC sincronizado [MM:SS.cc], incluidos separadores y créditos")
    print(f"Créditos   : {'desactivados' if args.no_credits else 'añadidos al final de ©lyr cuando existan'}")
    print("Protección : archivo temporal + verificación de mdat + verificación de otros tags")
    print("Respaldos  : desactivados; no se crean copias persistentes")
    print(f"Procesamiento: secuencial | lote={args.batch_size} archivo(s) | pausa={args.batch_pause_seconds:g} s")
    print(f"Log        : {log_path}")
    print("-" * 92)

    logger.info("Inicio | modo=%s | raíz=%s | créditos=%s", mode, root, not args.no_credits)
    logger.info("Log completo: %s", log_path)

    def on_walk_error(exc: OSError) -> None:
        failed_path = str(Path(exc.filename).resolve()) if getattr(exc, "filename", None) else str(root)
        logger.error(
            "ERROR explorando directorio | ruta=%s | %s: %s",
            failed_path, type(exc).__name__, exc,
        )
        print(f"[ERROR DIRECTORIO] {failed_path}")
        print(f"                  {type(exc).__name__}: {exc}")

    files = list(iter_m4a_files(root, onerror=on_walk_error))
    # El orden estable hace reproducibles los lotes entre ejecuciones.
    files.sort(key=lambda path: str(path.relative_to(root)).casefold())
    counts = {"PENDIENTE": 0, "ACTUALIZADO": 0, "SIN CAMBIOS": 0, "SIN TTML": 0, "ERROR": 0}
    batch_count = (len(files) + args.batch_size - 1) // args.batch_size

    logger.info(
        "Plan de lotes | archivos=%d | lotes=%d | tamaño_lote=%d | pausa_segundos=%g | concurrencia=1",
        len(files), batch_count, args.batch_size, args.batch_pause_seconds,
    )

    for batch_number, batch_start in enumerate(range(0, len(files), args.batch_size), start=1):
        batch_files = files[batch_start:batch_start + args.batch_size]
        batch_counts = {key: 0 for key in counts}
        batch_end_number = batch_start + len(batch_files)

        print(f"LOTE {batch_number}/{batch_count} | archivos {batch_start + 1}-{batch_end_number} de {len(files)} | ejecución secuencial")
        logger.info(
            "LOTE %d/%d INICIO | rango=%d-%d | total_biblioteca=%d",
            batch_number, batch_count, batch_start + 1, batch_end_number, len(files),
        )

        for file_in_batch, m4a_path in enumerate(batch_files, start=1):
            display = relative_display(m4a_path, root)
            ttml_path: Path | None = None
            try:
                ttml_path = find_ttml_for_m4a(m4a_path)
                if ttml_path is None:
                    counts["SIN TTML"] += 1
                    batch_counts["SIN TTML"] += 1
                    print(f"[SIN TTML    ] [{file_in_batch}/{len(batch_files)}] {display}")
                    logger.info("SIN TTML | M4A=%s", m4a_path.resolve())
                    continue

                lyrics, skipped, credits = ttml_to_lrc(ttml_path, include_credits=not args.no_credits)
                audio = MP4(str(m4a_path))
                current_lyrics = get_lyrics_value(audio.tags)
                del audio  # Libera la referencia antes de procesar el siguiente M4A.
                if current_lyrics == lyrics:
                    counts["SIN CAMBIOS"] += 1
                    batch_counts["SIN CAMBIOS"] += 1
                    print(f"[SIN CAMBIOS ] [{file_in_batch}/{len(batch_files)}] {display}")
                    logger.info("SIN CAMBIOS | M4A=%s | TTML=%s", m4a_path.resolve(), ttml_path.resolve())
                    continue

                first_line = next((line for line in lyrics.splitlines() if line.startswith("[")), "(sin texto)")
                print(f"[PENDIENTE   ] [{file_in_batch}/{len(batch_files)}] {display}")
                print(f"                {first_line[:150]}")
                if credits:
                    print(f"                Créditos/metadatos: {len(credits)} campo(s)")
                    for credit_line in credits[:5]:
                        print(f"                  {credit_line[:150]}")
                    if len(credits) > 5:
                        print(f"                  ... y {len(credits) - 5} campo(s) más")
                if skipped:
                    print(f"                Aviso: {skipped} párrafo(s) p sin begin fueron omitidos.")

                if args.apply:
                    write_updated_file(m4a_path, lyrics)
                    counts["ACTUALIZADO"] += 1
                    batch_counts["ACTUALIZADO"] += 1
                    print("                Resultado: actualizado ©lyr; audio y demás tags verificados antes del reemplazo.")
                    logger.info("ACTUALIZADO | M4A=%s | TTML=%s", m4a_path.resolve(), ttml_path.resolve())
                else:
                    counts["PENDIENTE"] += 1
                    batch_counts["PENDIENTE"] += 1
                    logger.info("PENDIENTE (simulación) | M4A=%s | TTML=%s", m4a_path.resolve(), ttml_path.resolve())
            except Exception as exc:
                counts["ERROR"] += 1
                batch_counts["ERROR"] += 1
                m4a_full = str(m4a_path.resolve())
                ttml_full = str(ttml_path.resolve()) if ttml_path is not None else "No localizado / no determinado"
                print(f"[ERROR       ] {m4a_full}")
                print(f"                TTML: {ttml_full}")
                print(f"                {type(exc).__name__}: {exc}")
                # El log incluye el traceback completo para identificar la línea que falló.
                logger.exception(
                    "ERROR procesando archivo | M4A=%s | TTML=%s",
                    m4a_full, ttml_full,
                )

        batch_summary = " | ".join(f"{key}={batch_counts[key]}" for key in counts)
        print(f"FIN LOTE {batch_number}/{batch_count} | {batch_summary}")
        logger.info("LOTE %d/%d FIN | %s", batch_number, batch_count, batch_summary)
        for handler in logger.handlers:
            handler.flush()

        # Descanso breve configurable entre lotes. Dentro de cada lote y entre
        # todos sus archivos sólo existe un único flujo de trabajo activo.
        if batch_number < batch_count and args.batch_pause_seconds > 0:
            print(f"Pausa entre lotes: {args.batch_pause_seconds:g} s")
            time.sleep(args.batch_pause_seconds)

    print("-" * 92)
    print("RESUMEN")
    print(f"  M4A encontrados : {len(files)}")
    print(f"  Lotes           : {batch_count} (tamaño máximo {args.batch_size})")
    print("  Concurrencia    : 1 (secuencial)")
    for label in ("PENDIENTE", "ACTUALIZADO", "SIN CAMBIOS", "SIN TTML", "ERROR"):
        print(f"  {label:<14}: {counts[label]}")
    print(f"  Log             : {log_path}")
    if args.apply:
        print("\nProceso terminado. No se crearon respaldos persistentes.")
        print("El archivo temporal de trabajo se elimina al terminar cada actualización.")
    else:
        print("\nNo se modificó ningún archivo. Revisa los resultados; para escribir, agrega -Apply al lanzador.")

    logger.info(
        "Resumen | M4A=%d | LOTES=%d | PENDIENTE=%d | ACTUALIZADO=%d | SIN_CAMBIOS=%d | SIN_TTML=%d | ERROR=%d",
        len(files), batch_count, counts["PENDIENTE"], counts["ACTUALIZADO"],
        counts["SIN CAMBIOS"], counts["SIN TTML"], counts["ERROR"],
    )
    for handler in list(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)

    return 1 if counts["ERROR"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
