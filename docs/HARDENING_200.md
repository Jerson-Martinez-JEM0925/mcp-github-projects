# Registro de 200 mejoras del MCP de GitHub Projects

Fecha: 2026-08-13
Alcance: repositorio raíz (`server.py`, `tools/`, `services/`, `clients/`, `graphql/`, `models/`)
Leyenda: **A** aplicado en esta ola · **E** existente/verificado · **P** pendiente de una fase posterior.

> Este registro evita confundir inventario con implementación. Las mejoras marcadas **A** tienen cambios en código y validación de sintaxis dentro de Docker. Las marcadas **E** ya estaban presentes y se conservaron. Las **P** son especificaciones concretas priorizadas; no se declaran terminadas.

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

11. **A** Limitar `timeout_seconds` a 1–120 segundos; evita bloqueos indefinidos o valores accidentales.
12. **A** Limitar `retry_attempts` a 0–5; evita tormentas de reintentos.
13. **A** Limitar `retry_delay_seconds` a 0–60; evita esperas operativas excesivas.
14. **A** Limitar `cache_ttl_hours` a 1–720; evita cachés eternas o expiración inmediata.
15. **A** Limitar `project_number` a un rango positivo razonable.
16. **A** Limitar `max_items` a 1–1,000; evita respuestas gigantes.
17. **A** Limitar `page_size` al máximo soportado por Projects V2.
18. **A** Limitar la salida capturada de gh CLI; evita amplificación de memoria.
19. **A** Hacer configurable la ruta de caché sin hardcodear el entorno.
20. **P** Validar `org_name` y `repo_name` contra un patrón GitHub login seguro.

## 3. Autenticación y tokens

21. **A** Recortar whitespace de `GITHUB_TOKEN` y `GH_TOKEN`; evita tokens inválidos por newline.
22. **A** Decodificar la salida de `gh auth token` con reemplazo seguro; evita errores por bytes inválidos.
23. **E** Mantener prioridad explícita GITHUB_TOKEN → GH_TOKEN → gh CLI.
24. **E** Omitir validación de scopes clásicos para fine-grained PATs que no exponen el header.
25. **E** No imprimir tokens durante la autenticación.
26. **P** Validar formato/prefijo de token sin registrar su valor.
27. **P** Añadir expiración visible del token sin revelar el secreto.
28. **P** Separar permisos requeridos por herramienta en vez de exigir el máximo global.
29. **P** Revalidar token ante respuestas 401 durante una sesión larga.
30. **P** Añadir soporte de GitHub App installation tokens con renovación controlada.

## 4. Cliente GraphQL: transporte

31. **A** Convertir errores de red `httpx.RequestError` en errores MCP tipados y acotados.
32. **A** Tratar 408, 425, 429 y 5xx como indisponibilidad temporal estructurada.
33. **A** Tolerar headers de rate limit malformados sin lanzar `ValueError` accidental.
34. **A** Acotar mensajes GraphQL para que no amplifiquen respuestas del proveedor.
35. **A** Aplicar reintentos configurables y backoff exponencial sólo a lecturas timeout.
36. **E** No reintentar mutaciones cuyo resultado pueda ser desconocido.
37. **E** Exponer `X-Request-Id` en errores para soporte.
38. **P** Respetar `Retry-After` en respuestas 429 antes del siguiente intento.
39. **P** Añadir límites separados de conexión, lectura y escritura HTTP.
40. **P** Reutilizar un `httpx.AsyncClient` por contexto para reducir handshakes.

## 5. Cliente GraphQL: respuestas y semántica

41. **E** Envolver el resultado con `data` y `request_id` de forma consistente.
42. **E** Detectar errores GraphQL aunque el HTTP status sea 200.
43. **P** Validar el tipo de `data` antes de entregarlo al servicio.
44. **P** Normalizar errores `FORBIDDEN`, `NOT_FOUND` y `UNAUTHORIZED` del payload.
45. **P** Detectar respuestas parciales `data + errors` y reportar degradación.
46. **P** Registrar métricas de costo GraphQL sin guardar query ni variables sensibles.
47. **P** Rechazar queries vacías antes de abrir conexión.
48. **P** Rechazar variables no serializables con error de validación.
49. **P** Añadir allowlist de operaciones GraphQL conocidas.
50. **P** Añadir pruebas contractuales contra fixtures de respuestas reales anonimizadas.

## 6. Cliente gh CLI: ejecución

51. **A** Redactar y acotar errores de gh CLI antes de guardarlos en excepciones.
52. **A** Evitar incluir argumentos completos potencialmente sensibles en timeout errors.
53. **A** Decodificar stdout/stderr con `errors="replace"` para robustez.
54. **A** Rechazar salida de subprocess mayor al límite configurado.
55. **A** Soportar variables GraphQL tipadas usando `-f` para strings y `-F` para JSON.
56. **E** Usar `create_subprocess_exec` sin shell; evita inyección de comandos.
57. **E** Matar y esperar el proceso cuando expira el timeout.
58. **P** Limpiar el entorno heredado y pasar sólo variables necesarias.
59. **P** Añadir `cwd` controlado para comandos que dependan del repositorio.
60. **P** Distinguir `gh` inexistente, no autenticado y comando inválido.

## 7. Caché y persistencia local

61. **A** Escribir la caché mediante archivo temporal + `os.replace`; evita corrupción parcial.
62. **A** Aplicar permisos `0600` al temporal y al archivo final.
63. **A** Considerar inválida una entrada cuyo timestamp esté en el futuro.
64. **A** Usar `cache_path` validado desde settings.
65. **A** Evitar reutilizar caché de otra organización o número de proyecto.
66. **E** Ignorar JSON corrupto o con claves requeridas ausentes.
67. **E** Validar la caché con `ProjectMetadata` de Pydantic.
68. **P** Añadir lock de proceso para dos descubrimientos concurrentes.
69. **P** Añadir versión de esquema a la caché para migraciones futuras.
70. **P** Invalidar caché automáticamente ante errores de campo desconocido.

## 8. Descubrimiento de Projects

71. **A** Rechazar caché incompatible antes de servirla.
72. **E** Resolver IDs dinámicos en lugar de hardcodearlos.
73. **E** Cachear opciones de campos para reducir llamadas.
74. **E** Permitir descubrimiento forzado.
75. **P** Devolver fecha de descubrimiento y edad de caché al usuario.
76. **P** Incluir project title y owner type en metadata.
77. **P** Validar que cada field node tenga ID, nombre y data type.
78. **P** Detectar campos duplicados por nombre y reportar conflicto.
79. **P** Soportar paginación de fields si GitHub supera 50 campos.
80. **P** Añadir estrategia stale-while-revalidate para lecturas no críticas.

## 9. Listado y paginación de items

81. **A** Usar `page_size` configurable en vez de constante oculta.
82. **A** Detener paginación si `hasNextPage` no trae cursor.
83. **A** Detener paginación si GitHub repite el mismo cursor.
84. **A** Tolerar nodos GraphQL no diccionario.
85. **A** Tolerar contenido nulo o de tipo inesperado.
86. **A** Tolerar nodos de assignee/label malformados.
87. **A** Aumentar la ventana de labels, assignees y field values a 100.
88. **E** Aplicar filtros AND entre criterios distintos.
89. **E** Aplicar labels con semántica any-match documentada.
90. **P** Añadir paginación de salida MCP con cursor local y `has_more`.

## 10. Filtros y validación de Projects

91. **A** Validar operador de fecha en la capa de servicio, no sólo en la herramienta.
92. **A** Manejar fechas de item no parseables sin romper toda la lista.
93. **E** Validar Status contra opciones descubiertas.
94. **E** Validar Priority contra opciones descubiertas.
95. **P** Usar comparación case-insensitive opcional para nombres de opciones.
96. **P** Validar listas de labels no vacías y sin duplicados.
97. **P** Añadir filtro `content_type` para separar Issue/DraftIssue.
98. **P** Añadir filtro `archived` explícito.
99. **P** Añadir filtros por rango de estimate.
100. **P** Añadir orden estable por issue number/title/due date.

## 11. Modelos Pydantic

101. **A** Reemplazar listas mutables `default=[]` por `default_factory=list`.
102. **A** Aplicar límites de configuración mediante `Field`.
103. **E** Mantener envelopes `ToolSuccess` y `ToolError` uniformes.
104. **E** Mantener tipos explícitos para item, field y option.
105. **P** Añadir URLs como `HttpUrl` donde corresponda.
106. **P** Añadir tipos `date` en inputs y serializar ISO al borde.
107. **P** Añadir enums para error types, estados y operadores.
108. **P** Prohibir campos extra en inputs críticos.
109. **P** Añadir validadores de whitespace para títulos y nombres.
110. **P** Añadir validadores de unicidad para assignees y labels.

## 12. Issue service

111. **A** Reemplazar `tempfile.mktemp` por `NamedTemporaryFile(delete=False)` seguro.
112. **A** Eliminar temporales con manejo explícito de `FileNotFoundError`.
113. **A** Normalizar y deduplicar labels antes de invocar gh.
114. **A** Normalizar y deduplicar assignees antes de invocar gh.
115. **A** Usar parser JSON tipado para `gh issue view`.
116. **E** Mantener semántica de reemplazo para labels y assignees.
117. **E** Detectar DraftIssue al cerrar.
118. **E** Extraer issue number desde URL de creación.
119. **P** Validar URL contra owner/repo configurados.
120. **P** Añadir operación de edición de título como método del servicio.

## 13. Operaciones de Issues

121. **E** Crear issues con body-file para Unicode y cuerpos largos.
122. **E** Permitir milestone, labels y assignees al crear.
123. **E** Mantener cierre idempotente cuando ya está cerrado.
124. **E** Mantener reapertura idempotente.
125. **P** Clasificar errores HTTP 422 de labels/milestone como validación.
126. **P** Clasificar 404 de issue como not_found sin depender sólo del texto.
127. **P** Añadir `state_reason` para completed/not planned.
128. **P** Añadir edición de comentarios con control de autor.
129. **P** Añadir borrado de comentarios sólo con confirmación explícita.
130. **P** Añadir soporte de issues transferidos entre repositorios.

## 14. Sub-issues y relaciones

131. **E** Resolver IDs de padre e hijo antes de mutar.
132. **E** Listar sub-issues con estado y assignees.
133. **E** Reportar si el hijo no pertenece al padre.
134. **P** Detectar ciclos antes de crear relación.
135. **P** Rechazar padre e hijo iguales.
136. **P** Hacer add/remove idempotentes.
137. **P** Añadir paginación de sub-issues.
138. **P** Añadir profundidad máxima al árbol.
139. **P** Detectar relaciones huérfanas en auditoría.
140. **P** Añadir operación para reordenar sub-issues.

## 15. Campos de Project

141. **E** Resolver option ID desde nombre visible.
142. **E** Soportar SINGLE_SELECT, DATE, NUMBER y TEXT.
143. **E** Validar formato ISO para fechas.
144. **E** Validar números convertibles a float.
145. **P** Soportar clearing explícito de campo con `null`.
146. **P** Soportar ITERATION con fecha de inicio/fin.
147. **P** Validar rango de Estimate también en `ProjectService`.
148. **P** Tratar nombres de campo con normalización Unicode.
149. **P** Devolver valor anterior y nuevo en mutaciones.
150. **P** Verificar mutation payload antes de confirmar éxito.

## 16. Mutaciones y consistencia

151. **E** No reintentar mutaciones automáticamente.
152. **E** Reportar partial success al crear issue y agregarlo al board.
153. **E** Devolver item ID para recuperación manual.
154. **P** Implementar idempotency key basada en request hash.
155. **P** Añadir reconciliación post-mutación para detectar timeouts ambiguos.
156. **P** Añadir compensación opcional para issue creado sin item.
157. **P** Guardar plan de mutación antes de ejecutar operaciones múltiples.
158. **P** Permitir `dry_run` en bulk operations.
159. **P** Limitar tamaño de bulk por configuración.
160. **P** Añadir rollback sólo donde GitHub permita reversión segura.

## 17. Bulk y workflows

161. **E** Reportar resultado por elemento en bulk update.
162. **E** Reportar fallos parciales en bulk assign/close.
163. **E** Mantener estrategias de sprint planning documentadas.
164. **P** Ejecutar bulk con concurrencia limitada y semaphore.
165. **P** Añadir cancelación cooperativa entre elementos.
166. **P** Añadir contador de rate-limit estimado por lote.
167. **P** Añadir modo dry-run a `complete_issue`.
168. **P** Hacer workflows reanudables desde checkpoints.
169. **P** Generar correlation ID por workflow.
170. **P** Evitar que `except Exception` oculte qué paso falló.

## 18. Errores y experiencia MCP

171. **A** Acotar mensajes de excepción devueltos al cliente MCP.
172. **E** Mantener cinco categorías estables de error.
173. **E** Incluir sugerencias accionables por categoría.
174. **E** Incluir request ID cuando GitHub lo entrega.
175. **P** Añadir código estable por error además de `error_type`.
176. **P** Añadir `retryable: bool` en `ToolError`.
177. **P** Añadir `details` estructurados sin volcar stderr crudo.
178. **P** Añadir nombre de herramienta al envelope de error.
179. **P** Traducir mensajes de UX al idioma configurado sin tocar logs.
180. **P** Añadir documentación de recuperación específica por herramienta.

## 19. Seguridad, observabilidad y operación

181. **A** Redactar tokens en patrones Bearer/token/password.
182. **A** Limitar texto de diagnóstico para evitar log injection/amplification.
183. **E** Ejecutar gh sin shell.
184. **E** Ejecutar el contenedor con usuario no root.
185. **P** Añadir logging JSON con tool, duración, request ID y resultado.
186. **P** Añadir métricas de latencia por cliente y herramienta.
187. **P** Añadir contador de errores por categoría.
188. **P** Añadir health probe offline que no llame a mutaciones.
189. **P** Añadir tracing opcional con propagación de correlation ID.
190. **P** Añadir política de retención para caché y logs temporales.

## 20. Pruebas, CI y documentación

191. **A** Validar compilación de ambas copias dentro de Docker.
192. **A** Mantener registro único de hallazgos y solución.
193. **E** Tener pruebas unitarias de auth, config, errores y modelos en backend.
194. **P** Añadir pruebas unitarias para `hardening.py`.
195. **P** Añadir pruebas de timeout y kill del CLI.
196. **P** Añadir pruebas de paginación con cursor nulo/repetido.
197. **P** Añadir fixtures de GraphQL 401/403/429/5xx/partial data.
198. **P** Añadir contract tests de todos los envelopes FastMCP.
199. **P** Añadir pipeline que construya y ejecute smoke test de `mcp/Dockerfile`.
200. **P** Actualizar documentación de la ruta real de ejecución y sincronización de las dos copias.

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
