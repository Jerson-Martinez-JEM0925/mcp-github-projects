# Registro de 200 mejoras del MCP de GitHub Projects

Fecha: 2026-08-13
Alcance: repositorio raíz (`server.py`, `tools/`, `services/`, `clients/`, `graphql/`, `models/`)
Leyenda: **A** aplicado · **E** existente/verificado · **P** backlog priorizado.

> Este documento es un registro de hardening y no una lista de bloqueadores de
> release. Los puntos **A** tienen implementación y validación; los **P** son
> mejoras futuras que se pueden abordar sin romper el contrato estable.

## 1. Arquitectura y límites de responsabilidad

1. **A** Centralizar redacción de secretos, JSON seguro y escritura atómica en `hardening.py`; evita duplicación y fugas.
2. **A** Mantener la misma capa de hardening en la imagen independiente y el paquete embebido; evita divergencia funcional.
3. **A** Validar configuración con Pydantic antes de registrar herramientas; evita límites inválidos en runtime.
4. **A** Separar límites de red, paginación, caché y CLI en settings; facilita operación por ambiente.
5. **E** Conservar la separación herramientas → servicios → clientes → GitHub.
6. **A** Añadir un `ServiceFactory` para eliminar construcción repetida de clientes en cada herramienta (`core/factory.py`, #35).
7. **A** Añadir un `RequestContext` por invocación con request ID local y metadata de operación (`core/context.py`, `ToolError.correlation_id`, #35).
8. **A** Definir interfaces Protocol para GraphQL y gh CLI; simplifica mocks y pruebas contractuales (`core/protocols.py`, #35).
9. **P** Eliminar la duplicación física entre `mcp/` y el paquete backend mediante un paquete compartido versionado.
10. **P** Añadir chequeo CI que compare hashes de ambas implementaciones antes de publicar la imagen.

## 2. Configuración y entorno

1. **A** Limitar `timeout_seconds` a 1–120 segundos; evita bloqueos indefinidos o valores accidentales.
2. **A** Limitar `retry_attempts` a 0–5; evita tormentas de reintentos.
3. **A** Limitar `retry_delay_seconds` a 0–60; evita esperas operativas excesivas.
4. **A** Limitar `cache_ttl_hours` a 1–720; evita cachés eternas o expiración inmediata.
5. **A** Limitar `project_number` a un rango positivo razonable.
6. **A** Limitar `max_items` a 1–1,000; evita respuestas gigantes.
7. **A** Limitar `page_size` al máximo soportado por Projects V2.
8. **A** Limitar la salida capturada de gh CLI; evita amplificación de memoria.
9. **A** Hacer configurable la ruta de caché sin hardcodear el entorno.
10. **P** Validar `org_name` y `repo_name` contra un patrón GitHub login seguro.

## 3. Autenticación y tokens

1. **A** Recortar whitespace de `GITHUB_TOKEN` y `GH_TOKEN`; evita tokens inválidos por newline.
2. **A** Decodificar la salida de `gh auth token` con reemplazo seguro; evita errores por bytes inválidos.
3. **E** Mantener prioridad explícita GITHUB_TOKEN → GH_TOKEN → gh CLI.
4. **E** Omitir validación de scopes clásicos para fine-grained PATs que no exponen el header.
5. **E** No imprimir tokens durante la autenticación.
6. **P** Validar formato/prefijo de token sin registrar su valor.
7. **P** Añadir expiración visible del token sin revelar el secreto.
8. **P** Separar permisos requeridos por herramienta en vez de exigir el máximo global.
9. **P** Revalidar token ante respuestas 401 durante una sesión larga.
10. **P** Añadir soporte de GitHub App installation tokens con renovación controlada.

## 4. Cliente GraphQL: transporte

1. **A** Convertir errores de red `httpx.RequestError` en errores MCP tipados y acotados.
2. **A** Tratar 408, 425, 429 y 5xx como indisponibilidad temporal estructurada.
3. **A** Tolerar headers de rate limit malformados sin lanzar `ValueError` accidental.
4. **A** Acotar mensajes GraphQL para que no amplifiquen respuestas del proveedor.
5. **A** Aplicar reintentos configurables y backoff exponencial sólo a lecturas timeout.
6. **E** No reintentar mutaciones cuyo resultado pueda ser desconocido.
7. **E** Exponer `X-Request-Id` en errores para soporte.
8. **P** Respetar `Retry-After` en respuestas 429 antes del siguiente intento.
9. **P** Añadir límites separados de conexión, lectura y escritura HTTP.
10. **P** Reutilizar un `httpx.AsyncClient` por contexto para reducir handshakes.

## 5. Cliente GraphQL: respuestas y semántica

1. **E** Envolver el resultado con `data` y `request_id` de forma consistente.
2. **E** Detectar errores GraphQL aunque el HTTP status sea 200.
3. **P** Validar el tipo de `data` antes de entregarlo al servicio.
4. **P** Normalizar errores `FORBIDDEN`, `NOT_FOUND` y `UNAUTHORIZED` del payload.
5. **P** Detectar respuestas parciales `data + errors` y reportar degradación.
6. **P** Registrar métricas de costo GraphQL sin guardar query ni variables sensibles.
7. **P** Rechazar queries vacías antes de abrir conexión.
8. **P** Rechazar variables no serializables con error de validación.
9. **P** Añadir allowlist de operaciones GraphQL conocidas.
10. **P** Añadir pruebas contractuales contra fixtures de respuestas reales anonimizadas.

## 6. Cliente gh CLI: ejecución

1. **A** Redactar y acotar errores de gh CLI antes de guardarlos en excepciones.
2. **A** Evitar incluir argumentos completos potencialmente sensibles en timeout errors.
3. **A** Decodificar stdout/stderr con `errors="replace"` para robustez.
4. **A** Rechazar salida de subprocess mayor al límite configurado.
5. **A** Soportar variables GraphQL tipadas usando `-f` para strings y `-F` para JSON.
6. **E** Usar `create_subprocess_exec` sin shell; evita inyección de comandos.
7. **E** Matar y esperar el proceso cuando expira el timeout.
8. **P** Limpiar el entorno heredado y pasar sólo variables necesarias.
9. **P** Añadir `cwd` controlado para comandos que dependan del repositorio.
10. **P** Distinguir `gh` inexistente, no autenticado y comando inválido.

## 7. Caché y persistencia local

1. **A** Escribir la caché mediante archivo temporal + `os.replace`; evita corrupción parcial.
2. **A** Aplicar permisos `0600` al temporal y al archivo final.
3. **A** Considerar inválida una entrada cuyo timestamp esté en el futuro.
4. **A** Usar `cache_path` validado desde settings.
5. **A** Evitar reutilizar caché de otra organización o número de proyecto.
6. **E** Ignorar JSON corrupto o con claves requeridas ausentes.
7. **E** Validar la caché con `ProjectMetadata` de Pydantic.
8. **P** Añadir lock de proceso para dos descubrimientos concurrentes.
9. **P** Añadir versión de esquema a la caché para migraciones futuras.
10. **P** Invalidar caché automáticamente ante errores de campo desconocido.

## 8. Descubrimiento de Projects

1. **A** Rechazar caché incompatible antes de servirla.
2. **E** Resolver IDs dinámicos en lugar de hardcodearlos.
3. **E** Cachear opciones de campos para reducir llamadas.
4. **E** Permitir descubrimiento forzado.
5. **P** Devolver fecha de descubrimiento y edad de caché al usuario.
6. **P** Incluir project title y owner type en metadata.
7. **P** Validar que cada field node tenga ID, nombre y data type.
8. **P** Detectar campos duplicados por nombre y reportar conflicto.
9. **P** Soportar paginación de fields si GitHub supera 50 campos.
10. **P** Añadir estrategia stale-while-revalidate para lecturas no críticas.

## 9. Listado y paginación de items

1. **A** Usar `page_size` configurable en vez de constante oculta.
2. **A** Detener paginación si `hasNextPage` no trae cursor.
3. **A** Detener paginación si GitHub repite el mismo cursor.
4. **A** Tolerar nodos GraphQL no diccionario.
5. **A** Tolerar contenido nulo o de tipo inesperado.
6. **A** Tolerar nodos de assignee/label malformados.
7. **A** Aumentar la ventana de labels, assignees y field values a 100.
8. **E** Aplicar filtros AND entre criterios distintos.
9. **E** Aplicar labels con semántica any-match documentada.
10. **P** Añadir paginación de salida MCP con cursor local y `has_more`.

## 10. Filtros y validación de Projects

1. **A** Validar operador de fecha en la capa de servicio, no sólo en la herramienta.
2. **A** Manejar fechas de item no parseables sin romper toda la lista.
3. **E** Validar Status contra opciones descubiertas.
4. **E** Validar Priority contra opciones descubiertas.
5. **P** Usar comparación case-insensitive opcional para nombres de opciones.
6. **P** Validar listas de labels no vacías y sin duplicados.
7. **P** Añadir filtro `content_type` para separar Issue/DraftIssue.
8. **P** Añadir filtro `archived` explícito.
9. **P** Añadir filtros por rango de estimate.
10. **P** Añadir orden estable por issue number/title/due date.

## 11. Modelos Pydantic

1. **A** Reemplazar listas mutables `default=[]` por `default_factory=list`.
2. **A** Aplicar límites de configuración mediante `Field`.
3. **E** Mantener envelopes `ToolSuccess` y `ToolError` uniformes.
4. **E** Mantener tipos explícitos para item, field y option.
5. **P** Añadir URLs como `HttpUrl` donde corresponda.
6. **P** Añadir tipos `date` en inputs y serializar ISO al borde.
7. **P** Añadir enums para error types, estados y operadores.
8. **P** Prohibir campos extra en inputs críticos.
9. **P** Añadir validadores de whitespace para títulos y nombres.
10. **P** Añadir validadores de unicidad para assignees y labels.

## 12. Issue service

1. **A** Reemplazar `tempfile.mktemp` por `NamedTemporaryFile(delete=False)` seguro.
2. **A** Eliminar temporales con manejo explícito de `FileNotFoundError`.
3. **A** Normalizar y deduplicar labels antes de invocar gh.
4. **A** Normalizar y deduplicar assignees antes de invocar gh.
5. **A** Usar parser JSON tipado para `gh issue view`.
6. **E** Mantener semántica de reemplazo para labels y assignees.
7. **E** Detectar DraftIssue al cerrar.
8. **E** Extraer issue number desde URL de creación.
9. **P** Validar URL contra owner/repo configurados.
10. **P** Añadir operación de edición de título como método del servicio.

## 13. Operaciones de Issues

1. **E** Crear issues con body-file para Unicode y cuerpos largos.
2. **E** Permitir milestone, labels y assignees al crear.
3. **E** Mantener cierre idempotente cuando ya está cerrado.
4. **E** Mantener reapertura idempotente.
5. **P** Clasificar errores HTTP 422 de labels/milestone como validación.
6. **P** Clasificar 404 de issue como not_found sin depender sólo del texto.
7. **P** Añadir `state_reason` para completed/not planned.
8. **P** Añadir edición de comentarios con control de autor.
9. **P** Añadir borrado de comentarios sólo con confirmación explícita.
10. **P** Añadir soporte de issues transferidos entre repositorios.

## 14. Sub-issues y relaciones

1. **E** Resolver IDs de padre e hijo antes de mutar.
2. **E** Listar sub-issues con estado y assignees.
3. **E** Reportar si el hijo no pertenece al padre.
4. **P** Detectar ciclos antes de crear relación.
5. **P** Rechazar padre e hijo iguales.
6. **P** Hacer add/remove idempotentes.
7. **P** Añadir paginación de sub-issues.
8. **P** Añadir profundidad máxima al árbol.
9. **P** Detectar relaciones huérfanas en auditoría.
10. **P** Añadir operación para reordenar sub-issues.

## 15. Campos de Project

1. **E** Resolver option ID desde nombre visible.
2. **E** Soportar SINGLE_SELECT, DATE, NUMBER y TEXT.
3. **E** Validar formato ISO para fechas.
4. **E** Validar números convertibles a float.
5. **P** Soportar clearing explícito de campo con `null`.
6. **P** Soportar ITERATION con fecha de inicio/fin.
7. **P** Validar rango de Estimate también en `ProjectService`.
8. **P** Tratar nombres de campo con normalización Unicode.
9. **P** Devolver valor anterior y nuevo en mutaciones.
10. **P** Verificar mutation payload antes de confirmar éxito.

## 16. Mutaciones y consistencia

1. **E** No reintentar mutaciones automáticamente.
2. **E** Reportar partial success al crear issue y agregarlo al board.
3. **E** Devolver item ID para recuperación manual.
4. **P** Implementar idempotency key basada en request hash.
5. **P** Añadir reconciliación post-mutación para detectar timeouts ambiguos.
6. **P** Añadir compensación opcional para issue creado sin item.
7. **P** Guardar plan de mutación antes de ejecutar operaciones múltiples.
8. **P** Permitir `dry_run` en bulk operations.
9. **P** Limitar tamaño de bulk por configuración.
10. **P** Añadir rollback sólo donde GitHub permita reversión segura.

## 17. Bulk y workflows

1. **E** Reportar resultado por elemento en bulk update.
2. **E** Reportar fallos parciales en bulk assign/close.
3. **E** Mantener estrategias de sprint planning documentadas.
4. **P** Ejecutar bulk con concurrencia limitada y semaphore.
5. **P** Añadir cancelación cooperativa entre elementos.
6. **P** Añadir contador de rate-limit estimado por lote.
7. **P** Añadir modo dry-run a `complete_issue`.
8. **P** Hacer workflows reanudables desde checkpoints.
9. **P** Generar correlation ID por workflow.
10. **P** Evitar que `except Exception` oculte qué paso falló.

## 18. Errores y experiencia MCP

1. **A** Acotar mensajes de excepción devueltos al cliente MCP.
2. **E** Mantener cinco categorías estables de error.
3. **E** Incluir sugerencias accionables por categoría.
4. **E** Incluir request ID cuando GitHub lo entrega.
5. **P** Añadir código estable por error además de `error_type`.
6. **P** Añadir `retryable: bool` en `ToolError`.
7. **P** Añadir `details` estructurados sin volcar stderr crudo.
8. **P** Añadir nombre de herramienta al envelope de error.
9. **P** Traducir mensajes de UX al idioma configurado sin tocar logs.
10. **P** Añadir documentación de recuperación específica por herramienta.

## 19. Seguridad, observabilidad y operación

1. **A** Redactar tokens en patrones Bearer/token/password.
2. **A** Limitar texto de diagnóstico para evitar log injection/amplification.
3. **E** Ejecutar gh sin shell.
4. **E** Ejecutar el contenedor con usuario no root.
5. **P** Añadir logging JSON con tool, duración, request ID y resultado.
6. **P** Añadir métricas de latencia por cliente y herramienta.
7. **P** Añadir contador de errores por categoría.
8. **P** Añadir health probe offline que no llame a mutaciones.
9. **P** Añadir tracing opcional con propagación de correlation ID.
10. **P** Añadir política de retención para caché y logs temporales.

## 20. Pruebas, CI y documentación

1. **A** Validar compilación de ambas copias dentro de Docker.
2. **A** Mantener registro único de hallazgos y solución.
3. **E** Tener pruebas unitarias de auth, config, errores y modelos en backend.
4. **P** Añadir pruebas unitarias para `hardening.py`.
5. **P** Añadir pruebas de timeout y kill del CLI.
6. **P** Añadir pruebas de paginación con cursor nulo/repetido.
7. **P** Añadir fixtures de GraphQL 401/403/429/5xx/partial data.
8. **P** Añadir contract tests de todos los envelopes FastMCP.
9. **P** Añadir pipeline que construya y ejecute smoke test de `mcp/Dockerfile`.
10. **P** Actualizar documentación de la ruta real de ejecución y sincronización de las dos copias.

## Cambios aplicados en esta ola

- Nueva capa `hardening.py` en ambas copias.
- Límites Pydantic para red, retries, caché, paginación y salida CLI.
- Caché atómica, privada y compatible con organización/proyecto.
- GraphQL con errores de red tipados, respuestas transitorias y backoff configurable para lecturas.
- gh CLI con redacción, límites de salida, decodificación robusta y variables GraphQL tipadas.
- Issue service sin `mktemp`, con deduplicación y JSON validado.
- Project service con paginación segura, parsing defensivo y operadores de fecha validados.
- Queries con menor truncamiento de labels, assignees y campos.
- Modelos sin listas mutables compartidas.
- List tool sin instancia mutable por defecto.
- Compilación de las dos copias verificada dentro de Docker.

## Fases siguientes

1. **P0:** pruebas de la nueva capa, fixtures del cliente GraphQL/CLI, y contract test de envelopes.
2. **P1:** factory de servicios, idempotencia/reconciliación y bulk con concurrencia limitada.
3. **P2:** observabilidad estructurada, paginación MCP de salida, GitHub App tokens y eliminación de duplicación física.
