# Historial de cambios

Este registro resume la evolución de la línea 3.x. Cada versión cuenta con un commit descriptivo en el historial del proyecto. Las versiones 3.0.0 a 3.7.0 se documentan aquí para que los cambios sean trazables.

## [3.7.0] - Selector de carpeta de origen
- Añade el selector nativo de carpetas de Windows cuando no se especifica -Root.
- Conserva la selección explícita de carpeta desde PowerShell.
- Mantiene el procesamiento por lotes secuenciales, los logs y las validaciones del archivo temporal.

## [3.6.0] - Procesamiento secuencial por lotes
- Añade lotes ordenados y reproducibles, con tamaño configurable.
- Incorpora una pausa configurable entre lotes y resúmenes por lote.
- Mantiene una sola actualización activa; no utiliza escritura paralela.

## [3.5.0] - Logs detallados y eliminación de respaldos persistentes
- Elimina la creación de carpetas de respaldo persistentes.
- Conserva el archivo temporal completo y las verificaciones antes del reemplazo.
- Añade logs UTF-8 con rutas absolutas de M4A/TTML, tipo y mensaje de excepción y traceback completo.

## [3.4.0] - Compatibilidad de orden con OpenLyrics
- Representa los separadores entre secciones como líneas LRC vacías con timestamp para conservar su posición cronológica.
- Añade timestamps sintéticos al bloque final de créditos para que OpenLyrics no lo reordene detrás de las letras.

## [3.3.0] - Separación de secciones y metadatos de iTunes
- Conserva los límites de los bloques div.
- Extrae recursivamente los campos no vacíos de iTunesMetadata y omite elementos XML vacíos.

## [3.2.0] - Ajuste temporal de encabezados de sección
- Refina los timestamps de los encabezados para que aparezcan antes de la primera línea de cada sección.

## [3.1.0] - Encabezados itunes:songPart
- Extrae itunes:songPart de los contenedores TTML y coloca el nombre de la sección antes de sus letras como línea LRC sincronizada.

## [3.0.0] - Versión inicial
- Introduce la conversión de párrafos TTML sincronizados a líneas LRC.
- Empareja archivos M4A y TTML por nombre dentro de la misma carpeta.
- Añade simulación predeterminada y aplicación explícita mediante -Apply.
- Incorpora escritura temporal inicial y comprobaciones del contenido multimedia mdat.

## Notas de compatibilidad
- La línea 4.x fue descartada y no forma parte de este repositorio.
- La versión actual es 3.7.0.
