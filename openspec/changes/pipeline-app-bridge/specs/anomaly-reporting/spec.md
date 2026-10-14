## Purpose

Define cómo una anomalía marcada por el pipeline llega a la aplicación web: con qué identidad, con qué instantes de ingesta y análisis, y con qué garantías de entrega cuando la aplicación, el pipeline o el puente se caen.

## ADDED Requirements

### Requirement: Identidad estable de la anomalía
El pipeline SHALL asignar a cada anomalía un `anomaly_id` derivado del lote en el que llegó la línea y de su posición en ese lote, y SHALL usar ese mismo identificador en su base y en cada reporte a la aplicación, incluidos los reenvíos. Procesar dos veces el mismo lote SHALL producir los mismos identificadores y SHALL NOT crear una segunda fila para una anomalía no colapsada.

#### Scenario: Lote re-ejecutado tras una caída
- **WHEN** el pipeline persiste las anomalías de un lote, se cae antes de borrarlo y lo vuelve a procesar al arrancar
- **THEN** las anomalías no colapsadas de ese lote quedan una sola vez en la base, con el mismo `anomaly_id`

#### Scenario: Base del pipeline recreada
- **WHEN** se recrea la base del pipeline y llegan anomalías nuevas
- **THEN** sus `anomaly_id` no coinciden con los de ninguna anomalía reportada antes, y la aplicación las acepta

#### Scenario: Fila colapsada que suma ocurrencias
- **WHEN** una anomalía se suma a una fila colapsada que ya existe para su plantilla, nodo y hora
- **THEN** la fila conserva el `anomaly_id` de su primera ocurrencia

### Requirement: Instantes de ingesta y de análisis
El pipeline SHALL registrar para cada anomalía el instante en que el lote de su línea entró a la plataforma (`ingested_at`) y el instante en que la línea obtuvo su score (`analyzed_at`), ambos en UTC. `ingested_at` SHALL ser el instante de recepción del lote, no el timestamp escrito en la línea ni el que informa el collector. `analyzed_at` SHALL NOT ser anterior a `ingested_at`.

#### Scenario: Anomalía puntuada por el modelo
- **WHEN** una línea espera en la cola, se envía al detector y vuelve marcada
- **THEN** `analyzed_at − ingested_at` incluye la espera en la cola y el tiempo de inferencia

#### Scenario: Nodo con el reloj adelantado
- **WHEN** un collector envía líneas con un instante propio posterior a la hora real
- **THEN** los instantes de sus anomalías no dependen de ese reloj, y la aplicación no las rechaza por orden de instantes

### Requirement: Reporte de anomalías a la aplicación
El puente SHALL reportar cada anomalía de la base del pipeline a la aplicación con su `anomaly_id`, su nodo, el origen del log, el mensaje, la plantilla y su identidad cuando existan, el timestamp escrito en la línea cuando exista, el score, `ingested_at` y `analyzed_at`. Cada reporte SHALL declarar el modelo que produjo los scores (clave, revisión y umbral), SHALL llevar como máximo 500 anomalías y SHALL NOT mezclar anomalías de modelos distintos. El puente SHALL autenticarse con el token de servicio del pipeline.

#### Scenario: Instantes reales en la alerta
- **WHEN** una anomalía reportada abre una alerta en la aplicación
- **THEN** el tiempo de análisis de esa alerta es mayor que cero y corresponde a lo que tardó el pipeline

#### Scenario: Más de 500 anomalías pendientes
- **WHEN** hay 1200 anomalías pendientes del mismo modelo
- **THEN** el puente las envía en reportes de a lo sumo 500

#### Scenario: Cambio de modelo
- **WHEN** las anomalías pendientes fueron puntuadas por dos revisiones de modelo
- **THEN** el puente envía reportes separados, cada uno con su modelo

### Requirement: Entrega durable que no afecta la detección
El puente SHALL entregar cada anomalía al menos una vez y SHALL recordar hasta dónde entregó en la misma base donde están las anomalías. Una aplicación caída, lenta o que responde con un error de servidor SHALL NOT detener ni demorar el procesamiento de logs del pipeline, y las anomalías marcadas mientras tanto SHALL entregarse cuando la aplicación vuelva.

#### Scenario: Aplicación caída
- **WHEN** la aplicación no responde durante una hora y después vuelve
- **THEN** el pipeline siguió procesando logs durante esa hora, y las anomalías de ese período se entregan sin faltantes

#### Scenario: Reinicio del puente tras un envío sin confirmar
- **WHEN** el puente envía un reporte, la aplicación lo registra y el puente se reinicia antes de anotar el avance
- **THEN** el puente reenvía esas anomalías, la aplicación las responde como `DUPLICATE` y no se abre una segunda alerta

#### Scenario: Reinicio del pipeline
- **WHEN** el pipeline se reinicia con anomalías persistidas que todavía no se reportaron
- **THEN** esas anomalías se reportan igual

### Requirement: Tratamiento de rechazos
El puente SHALL leer el resultado de cada anomalía reportada. Una anomalía rechazada por la aplicación SHALL registrarse con su motivo y SHALL NOT reenviarse. Cuando la aplicación rechaza un reporte completo con un error de cliente, el puente SHALL registrar la respuesta, SHALL NOT avanzar sobre esas anomalías y SHALL volver a intentarlo, de modo que el problema no descarte anomalías en silencio.

#### Scenario: Nodo desconocido
- **WHEN** la aplicación rechaza anomalías con `UNKNOWN_HOST` porque su nodo no está en el inventario
- **THEN** el puente registra cuántas se rechazaron y por qué, no las reenvía y sigue con las siguientes

#### Scenario: Reporte rechazado entero
- **WHEN** la aplicación responde 400 a un reporte
- **THEN** el puente no saltea esas anomalías, registra el error y las entrega una vez corregida la causa

### Requirement: Reporte único de las filas colapsadas
El puente SHALL reportar una fila colapsada una sola vez, cuando aparece, con los datos de su primera ocurrencia. Las ocurrencias que se le suman después SHALL NOT generar reportes nuevos.

#### Scenario: Plantilla que se repite en la misma hora
- **WHEN** una plantilla sin contenido de seguridad se marca 200 veces en un nodo dentro de una hora
- **THEN** la aplicación recibe una anomalía por esa plantilla, nodo y hora
