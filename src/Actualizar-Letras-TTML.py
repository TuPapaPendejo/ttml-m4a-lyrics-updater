#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Actualiza exclusivamente el átomo ©lyr de archivos M4A usando TTML.

- Convierte begin de TTML a marcas LRC [MM:SS.cc].
- Inserta itunes:songPart como una línea LRC sincronizada antes de cada sección,
  usando el begin del contenedor y anticipándolo 0.01 s si hace falta para que
  el encabezado aparezca antes de la primera línea de la sección.
- Acepta reloj TTML (MM:SS.mmm / HH:MM:SS.mmm), segundos decimales
  sin unidad (por ejemplo 7.627) y offsets con unidades h, m, s y ms.
- No decodifica ni vuelve a codificar el audio.
- Trabaja sobre una copia temporal y sólo reemplaza el original tras validar:
  * hash idéntico del payload de todos los átomos MP4 mdat;
  * todos los demás tags Mutagen idénticos;
  * ©lyr exactamente igual al LRC generado.
- Crea una copia de seguridad completa antes del reemplazo.

El modo predeterminado es simulación. Para escribir, use --apply.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import struct
import sys
import tempfile
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
    """Extrae créditos y metadatos informativos presentes en el TTML.

    No inventa roles: songwriter se muestra como songwriter/autor, y sólo
    se etiqueta como compositor cuando el XML contiene ese campo.
    """
    result: list[str] = []
    seen: set[str] = set()

    def add_line(label: str, value: str) -> None:
        value = clean_metadata_value(value)
        if not value:
            return
        line = f"{label}: {value}"
        key = line.casefold()
        if key not in seen:
            seen.add(key)
            result.append(line)

    # Idioma y tipo de sincronización declarados en <tt>.
    language = get_attr_by_local(root, "lang")
    timing = get_attr_by_local(root, "timing")
    if language:
        add_line("Idioma TTML", language)
    if timing:
        add_line("Sincronización TTML", timing)

    # Duración declarada en <body dur="...">.
    for element in root.iter():
        if local_name(element.tag).casefold() == "body":
            duration = get_attr_by_local(element, "dur")
            if duration:
                add_line("Duración TTML", duration)
            break

    # Metadatos propios de Apple Music. Se recogen los textos de sus nodos
    # hoja; para contenedores como <songwriters>, se agrupan los nombres.
    for container in root.iter():
        if local_name(container.tag).casefold() != "itunesmetadata":
            continue

        leading_silence = get_attr_by_local(container, "leadingSilence")
        if leading_silence is not None:
            add_line("Silencio inicial", f"{leading_silence} s")

        grouped: dict[str, list[str]] = {}

        def walk_metadata(node: ET.Element, parent_group: str | None = None) -> None:
            node_name = local_name(node.tag)
            normalized_name = node_name.casefold()
            children = list(node)
            own_text = clean_metadata_value(node.text)
            if own_text:
                label_source = parent_group or node_name
                grouped.setdefault(metadata_label(label_source), []).append(own_text)

            if not children:
                return

            next_group = node_name
            for child in children:
                walk_metadata(child, next_group)

        for child in list(container):
            # El contenedor vacío <translations/> no aporta un valor que se
            # pueda representar en ©lyr, por lo que se omite.
            if local_name(child.tag).casefold() == "translations" and not list(child) and not clean_metadata_value(child.text):
                continue
            walk_metadata(child)

        for label, values in grouped.items():
            unique_values: list[str] = []
            for value in values:
                if value not in unique_values:
                    unique_values.append(value)
            add_line(label, "; ".join(unique_values))

    # Agentes TTML: conservamos el ID y el tipo (p. ej. v1/person) para que
    # el nombre no se confunda con un compositor o artista principal.
    for agent in root.iter():
        if local_name(agent.tag).casefold() != "agent":
            continue
        agent_id = get_attr_by_local(agent, "id")
        agent_type = get_attr_by_local(agent, "type")
        names: list[str] = []
        for child in agent.iter():
            if local_name(child.tag).casefold() == "name":
                value = collect_text(child)
                if value and value not in names:
                    name_type = get_attr_by_local(child, "type")
                    names.append(f"{value} ({name_type})" if name_type else value)
        if names:
            descriptor = "Agente TTML"
            if agent_id:
                descriptor += f" {agent_id}"
            if agent_type:
                descriptor += f" ({agent_type})"
            add_line(descriptor, "; ".join(names))

    # Secciones declaradas mediante itunes:songPart en <div>. Se conservan
    # sus rangos cuando el TTML proporciona begin y end.
    section_labels = {
        "verse": "Verso",
        "chorus": "Coro",
        "pre-chorus": "Pre-coro",
        "post-chorus": "Post-coro",
        "bridge": "Puente",
        "intro": "Intro",
        "outro": "Outro",
        "refrain": "Estribillo",
        "hook": "Hook",
        "interlude": "Interludio",
        "instrumental": "Instrumental",
        "breakdown": "Breakdown",
    }
    sections: dict[str, list[str]] = {}
    for element in root.iter():
        part = get_attr_by_local(element, "songPart")
        if not part:
            continue
        label = section_labels.get(part.casefold(), part)
        begin = get_attr_by_local(element, "begin")
        end = get_attr_by_local(element, "end")
        if begin and end:
            try:
                segment = f"{format_credit_time(begin)}–{format_credit_time(end)}"
            except ValueError:
                segment = f"{begin}–{end}"
        elif begin:
            try:
                segment = f"desde {format_credit_time(begin)}"
            except ValueError:
                segment = f"desde {begin}"
        else:
            segment = ""
        sections.setdefault(label, [])
        if segment and segment not in sections[label]:
            sections[label].append(segment)

    for label, ranges in sections.items():
        if ranges:
            add_line(f"Sección {label}", "; ".join(ranges))
        else:
            add_line("Sección", label)

    return result


def iter_lyric_paragraphs(root: ET.Element):
    """Recorre los párrafos p en orden XML e hereda itunes:songPart.

    Apple Music suele declarar songPart en un <div>. Cada elemento que declara
    el atributo inicia su propio bloque, incluso si repite un valor anterior.
    Un contenedor interno con songPart reemplaza al bloque heredado.
    """
    def walk(node: ET.Element, inherited_section: tuple[ET.Element, str] | None):
        song_part = get_attr_by_local(node, "songPart")
        if song_part is not None:
            value = clean_metadata_value(song_part)
            inherited_section = (node, value) if value else None

        if local_name(node.tag).casefold() == "p":
            yield node, inherited_section
            return

        for child in list(node):
            yield from walk(child, inherited_section)

    yield from walk(root, None)


def ttml_to_lrc(ttml_path: Path, include_credits: bool = True) -> tuple[str, int, list[str]]:
    try:
        raw = ttml_path.read_bytes()
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"No se pudo leer/analizar el TTML: {exc}") from exc

    lines: list[str] = []
    skipped_without_begin = 0
    last_section_element: ET.Element | None = None
    for element, section in iter_lyric_paragraphs(root):
        begin = element.attrib.get("begin")
        if begin is None:
            # Algunas variantes pueden declarar un p sin begin; no inventamos
            # una marca de tiempo ni heredamos tiempos ambiguos del contenedor.
            skipped_without_begin += 1
            continue
        lyric = collect_text(element)
        if not lyric:
            continue

        lyric_seconds = parse_ttml_time(begin)

        # Inserta itunes:songPart como una línea LRC sincronizada. El instante
        # preferido es el begin del <div> que contiene la sección. Para que el
        # encabezado quede antes de su primera línea incluso cuando los tiempos
        # del div y del primer p coinciden o se redondean igual, se usa como
        # límite superior el instante del primer p menos una centésima.
        # Si el div no tiene begin (o no se puede interpretar), se deriva del p.
        if section is not None and section[0] is not last_section_element:
            section_begin = get_attr_by_local(section[0], "begin")
            marker_seconds = lyric_seconds - Decimal("0.01")
            if section_begin:
                try:
                    marker_seconds = min(parse_ttml_time(section_begin), marker_seconds)
                except ValueError:
                    pass
            if marker_seconds < 0:
                marker_seconds = Decimal(0)
            if lines and lines[-1] != "":
                lines.append("")
            marker_timestamp = format_lrc_timestamp(marker_seconds)
            lines.append(f"{marker_timestamp}{section[1]}")

        last_section_element = section[0] if section is not None else None

        timestamp = format_lrc_timestamp(lyric_seconds)
        lines.append(f"{timestamp}{lyric}")

    if not any(line.startswith("[") for line in lines):
        detail = f"; {skipped_without_begin} elemento(s) p sin begin" if skipped_without_begin else ""
        raise ValueError(f"El TTML no contiene párrafos p con begin y texto{detail}")

    # Se conserva el orden original de los párrafos TTML; esto evita reordenar
    # líneas que se solapan temporalmente.
    credits = extract_ttml_credits(root) if include_credits else []
    if credits:
        lines.extend(["", "[Créditos y metadatos TTML]"])
        lines.extend(credits)
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
        return None
    for name in (m4a_path.stem + ".ttml", m4a_path.name + ".ttml"):
        found = names.get(name.casefold())
        if found is not None:
            return found
    return None


def iter_m4a_files(root: Path):
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(BACKUP_PREFIX)]
        for filename in files:
            if filename.lower().endswith(".m4a"):
                yield Path(current) / filename


def relative_display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def write_updated_file(m4a_path: Path, lyrics: str, root: Path, backup_root: Path) -> None:
    # 1. Captura estado inicial; si no podemos validarlo, no tocamos el archivo.
    original_audio = MP4(str(m4a_path))
    before_tags = snapshot_non_lyrics_tags(original_audio.tags)
    before_mdat = mdat_payload_sha256(m4a_path)
    original_lyrics = get_lyrics_value(original_audio.tags)
    if original_lyrics == lyrics:
        return

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".ttml_lyrics_", suffix=".tmp", dir=str(m4a_path.parent), delete=False) as temp_file:
            temp_name = temp_file.name
        temp_path = Path(temp_name)
        shutil.copy2(m4a_path, temp_path)

        edited_audio = MP4(str(temp_path))
        if edited_audio.tags is None:
            edited_audio.add_tags()
        edited_audio.tags[LYRICS_KEY] = [lyrics]
        edited_audio.save()

        # 2. Valida el temporal antes de sustituir el original.
        check_audio = MP4(str(temp_path))
        if get_lyrics_value(check_audio.tags) != lyrics:
            raise ValueError("La lectura posterior no coincide con la letra LRC escrita en ©lyr")
        if snapshot_non_lyrics_tags(check_audio.tags) != before_tags:
            raise ValueError("Mutagen detectó cambios en etiquetas distintas de ©lyr; se cancela el reemplazo")
        after_mdat = mdat_payload_sha256(temp_path)
        if after_mdat != before_mdat:
            raise ValueError("El contenido mdat cambió; se cancela el reemplazo para proteger el audio")

        # 3. Copia de seguridad completa y reemplazo atómico.
        relative = m4a_path.relative_to(root)
        backup_path = backup_root / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(m4a_path, backup_path)
        os.replace(temp_path, m4a_path)
        temp_name = None

        # 4. Verificación del archivo ya reemplazado. Si algo falla, restaura backup.
        final_audio = MP4(str(m4a_path))
        final_ok = (
            get_lyrics_value(final_audio.tags) == lyrics
            and snapshot_non_lyrics_tags(final_audio.tags) == before_tags
            and mdat_payload_sha256(m4a_path) == before_mdat
        )
        if not final_ok:
            shutil.copy2(backup_path, m4a_path)
            raise ValueError("Falló la verificación final; se restauró automáticamente la copia original")
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Actualiza sólo ©lyr de M4A a partir de TTML; por defecto simula sin escribir."
    )
    parser.add_argument("--root", required=True, help="Carpeta raíz que contiene la biblioteca M4A/TTML")
    parser.add_argument("--apply", action="store_true", help="Escribe los cambios; sin esta opción sólo simula")
    parser.add_argument("--no-credits", action="store_true", help="No añade créditos/metadatos TTML al final de ©lyr")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: La carpeta no existe o no es accesible: {root}", file=sys.stderr)
        return 2

    mode = "APLICAR CAMBIOS" if args.apply else "SIMULACIÓN (sin escribir)"
    print(f"\nBiblioteca : {root}")
    print(f"Modo       : {mode}")
    print("Formato    : [MM:SS.cc] con tiempos begin del TTML")
    print(f"Créditos   : {'desactivados' if args.no_credits else 'añadidos al final de ©lyr cuando existan'}")
    print("Protección : respaldo + verificación de mdat + verificación de otros tags")
    print("-" * 92)

    files = list(iter_m4a_files(root))
    counts = {"PENDIENTE": 0, "ACTUALIZADO": 0, "SIN CAMBIOS": 0, "SIN TTML": 0, "ERROR": 0}
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = root / f"{BACKUP_PREFIX}{timestamp}"
    backup_root_created = False

    for m4a_path in files:
        display = relative_display(m4a_path, root)
        try:
            ttml_path = find_ttml_for_m4a(m4a_path)
            if ttml_path is None:
                counts["SIN TTML"] += 1
                print(f"[SIN TTML    ] {display}")
                continue

            lyrics, skipped, credits = ttml_to_lrc(ttml_path, include_credits=not args.no_credits)
            audio = MP4(str(m4a_path))
            current_lyrics = get_lyrics_value(audio.tags)
            if current_lyrics == lyrics:
                counts["SIN CAMBIOS"] += 1
                print(f"[SIN CAMBIOS ] {display}")
                continue

            first_line = next((line for line in lyrics.splitlines() if line.startswith("[")), "(sin texto)")
            print(f"[PENDIENTE   ] {display}")
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
                if not backup_root_created:
                    backup_root.mkdir(parents=True, exist_ok=True)
                    backup_root_created = True
                write_updated_file(m4a_path, lyrics, root, backup_root)
                counts["ACTUALIZADO"] += 1
                print("                Resultado: actualizado ©lyr; audio y demás tags verificados.")
            else:
                counts["PENDIENTE"] += 1
        except Exception as exc:
            counts["ERROR"] += 1
            print(f"[ERROR       ] {display}")
            print(f"                {type(exc).__name__}: {exc}")

    print("-" * 92)
    print("RESUMEN")
    print(f"  M4A encontrados : {len(files)}")
    for label in ("PENDIENTE", "ACTUALIZADO", "SIN CAMBIOS", "SIN TTML", "ERROR"):
        print(f"  {label:<14}: {counts[label]}")
    if backup_root_created:
        print(f"  Respaldos       : {backup_root}")
    if args.apply:
        print("\nProceso terminado. Conserva los respaldos hasta comprobar los archivos en tu reproductor.")
    else:
        print("\nNo se modificó ningún archivo. Revisa los resultados; para escribir, agrega -Apply al lanzador.")

    return 1 if counts["ERROR"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
