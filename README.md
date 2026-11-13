# TTML M4A Lyrics Updater

**Versión actual:** 3.7.0  
**Plataforma:** Windows 10/11  
**Motor:** Python + Mutagen  
**Lanzador:** PowerShell

Utilidad para convertir letras sincronizadas TTML (especialmente las de Apple Music) a líneas LRC e incrustarlas en el átomo MP4 **©lyr** de archivos M4A. Está pensada para actualizar las letras sin decodificar ni volver a codificar el audio ALAC.

El programa no descarga letras ni obtiene TTML de servicios en línea. Procesa archivos TTML que ya existen localmente junto a sus archivos M4A.

## Funciones principales

- Convierte los tiempos begin de TTML a marcas LRC con centésimas: [MM:SS.cc].
- Admite tiempos TTML en reloj, segundos decimales y offsets con unidades.
- Extrae el texto de los párrafos y elementos internos, incluidos span.
- Convierte itunes:songPart (Intro, Verse, Chorus y otros) en encabezados LRC sincronizados.
- Conserva los separadores entre bloques div mediante líneas LRC vacías con timestamp, compatibles con el orden cronológico de OpenLyrics.
- Extrae recursivamente los campos no vacíos de iTunesMetadata, incluidos songwriters y otros metadatos disponibles.
- Añade los créditos al final con timestamps sintéticos para que OpenLyrics no los reordene fuera del bloque de letras. Esos tiempos no representan sincronización vocal original.
- Empareja Track.m4a con Track.ttml o Track.m4a.ttml y busca recursivamente en subcarpetas.
- Incluye simulación predeterminada, logs detallados, lotes secuenciales y selector nativo de carpetas.

## Protección del audio y los metadatos

El programa actualiza únicamente el átomo ©lyr mediante Mutagen. No decodifica ni recodifica ALAC.

En modo Apply, crea un archivo temporal completo junto al M4A, modifica el campo de letras y valida el resultado antes de sustituir el original:

1. Calcula SHA-256 del contenido de los átomos mdat.
2. Modifica ©lyr en el archivo temporal.
3. Verifica el contenido nuevo de ©lyr.
4. Compara los demás tags.
5. Comprueba que el SHA-256 de mdat permanezca idéntico.
6. Reemplaza el original mediante os.replace solo si pasan todas las comprobaciones.
7. Elimina el temporal.

No crea respaldos persistentes. El temporal requiere espacio libre aproximadamente equivalente al M4A que se procesa. La simulación debe revisarse antes de aplicar. Después de un reemplazo exitoso no hay restauración automática; conserva un respaldo externo de tu biblioteca.

## Requisitos

- Windows 10 u 11.
- Python 3.10 o posterior.
- Mutagen 1.48.1 (el lanzador comprueba e instala esta versión si hace falta).

Instalación manual opcional:

    py -3 -m pip install -r requirements.txt

## Instalación

Clona o descarga este repositorio. La estructura es:

    ttml-m4a-lyrics-updater/
    ├── src/
    │   ├── Actualizar-Letras-TTML.py
    │   └── Ejecutar-Actualizar-Letras.ps1
    ├── CHANGELOG.md
    ├── VERSION
    └── requirements.txt

Abre PowerShell en la carpeta src.

## Uso rápido

Simulación con selector interactivo de carpetas (modo predeterminado):

    .\Ejecutar-Actualizar-Letras.ps1

Simulación indicando una carpeta:

    .\Ejecutar-Actualizar-Letras.ps1 -Root 'D:\Pruebas'

Aplicar después de revisar la simulación:

    .\Ejecutar-Actualizar-Letras.ps1 -Apply

Aplicar indicando la carpeta:

    .\Ejecutar-Actualizar-Letras.ps1 -Root 'D:\Música\Apple Music' -Apply

## Parámetros de PowerShell

| Parámetro | Predeterminado | Descripción |
|---|---|---|
| -Root | Selector interactivo | Carpeta raíz; se recorren todas las subcarpetas. |
| -Apply | Desactivado | Escribe en los M4A. Sin este switch solo simula. |
| -NoCredits | Desactivado | Omite el bloque final de créditos y metadatos TTML. |
| -LogPath | Log en la carpeta raíz | Ruta absoluta o relativa para el log UTF-8. |
| -Python | Python detectado | Ejecutable de Python que se utilizará. |
| -BatchSize | 10 | Cantidad máxima de archivos por lote. Debe ser ≥ 1. |
| -BatchPauseSeconds | 1 | Pausa entre lotes, de 0 a 86400 segundos. |

Ejemplos:

Cinco archivos por lote:

    .\Ejecutar-Actualizar-Letras.ps1 -Root 'D:\Pruebas' -BatchSize 5

Aplicar con lotes de diez y pausa de dos segundos:

    .\Ejecutar-Actualizar-Letras.ps1 -Root 'D:\Música\Apple Music' -Apply -BatchSize 10 -BatchPauseSeconds 2

Omitir créditos:

    .\Ejecutar-Actualizar-Letras.ps1 -Root 'D:\Pruebas' -Apply -NoCredits

Log en ubicación personalizada:

    .\Ejecutar-Actualizar-Letras.ps1 -Root 'D:\Pruebas' -LogPath 'D:\Logs\TTML.log'

Seleccionar otro Python:

    .\Ejecutar-Actualizar-Letras.ps1 -Root 'D:\Pruebas' -Python 'C:\Python313\python.exe'

## Procesamiento secuencial por lotes

El programa nunca modifica varios M4A en paralelo. Ordena las rutas de forma reproducible, procesa cada archivo hasta completar sus verificaciones y reemplazo, escribe el resumen del lote y aplica la pausa configurada. Si se interrumpe la ejecución, puede volver a iniciarse; los archivos ya actualizados se identifican como SIN CAMBIOS.

## Logs

Por defecto, cada ejecución crea un log UTF-8 en la carpeta raíz. Los errores incluyen, cuando están disponibles, las rutas absolutas del M4A y TTML, el tipo y mensaje de excepción y el traceback completo de Python. Con un LogPath fijo los registros se agregan al mismo archivo.

## Versionado

La evolución de la línea 3.x está documentada en CHANGELOG.md. Los cambios de cada versión se describen allí. La versión 4.x quedó descartada y no forma parte de esta línea.

## Limitaciones

- El TTML debe existir localmente junto al M4A correspondiente.
- Los párrafos sin atributo begin no pueden sincronizarse y se contabilizan como omitidos.
- ©lyr es un campo de texto; algunos reproductores pueden presentar las marcas temporales o los créditos literalmente.
- Los tiempos de créditos son sintéticos y solo facilitan la presentación cronológica en OpenLyrics.
- La versión 3.7 incluye selector de carpetas nativo, no una GUI completa.

## Licencia

Este repositorio no incluye actualmente una licencia de código abierto. Todos los derechos permanecen reservados por el titular del proyecto hasta que se añada una licencia explícita.
