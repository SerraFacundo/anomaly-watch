## Purpose

Define qué mide el pipeline como latencia de análisis, cómo la agrega en ventanas de un minuto y cómo esas ventanas llegan a la aplicación web para la métrica de tiempo de respuesta.

## ADDED Requirements

### Requirement: Muestra de latencia de análisis
El pipeline SHALL tomar una muestra de latencia por cada registro que recibe un score, lo haya puntuado el modelo o lo haya resuelto la caché. La muestra SHALL ser el tiempo entre la recepción del lote del registro y el instante en que obtuvo su score. Los registros descartados antes de puntuarse y los que no se pudieron interpretar SHALL NOT aportar muestras.

#### Scenario: Registro resuelto por caché
- **WHEN** un registro obtiene su score de la caché de plantillas
- **THEN** aporta una muestra con su tiempo desde la recepción

#### Scenario: Registro descartado por sobrecarga
- **WHEN** la política de descarte elimina un registro antes de puntuarlo
- **THEN** ese registro no aporta ninguna muestra

### Requirement: Agregación en ventanas de un minuto
El pipeline SHALL agregar las muestras en ventanas de un minuto de reloj UTC, asignando cada muestra al minuto en que terminó su análisis. Cada ventana SHALL llevar la cantidad de muestras, la suma y el máximo en milisegundos, y un histograma con los límites de bucket vigentes en la aplicación, donde una muestra igual a un límite cae en el bucket de ese límite. La suma de los conteos del histograma SHALL ser igual a la cantidad de muestras.

#### Scenario: Muestras a ambos lados de un cambio de minuto
- **WHEN** un lote termina de analizar registros a las 14:05:59 y a las 14:06:01
- **THEN** cada muestra queda en la ventana de su minuto

#### Scenario: Muestra sobre un límite
- **WHEN** una muestra dura exactamente 1000 ms
- **THEN** se cuenta en el bucket cuyo límite superior es 1000

### Requirement: Agregación idempotente
Procesar dos veces el mismo lote SHALL NOT contar sus muestras dos veces.

#### Scenario: Lote re-ejecutado tras una caída
- **WHEN** el pipeline persiste un lote, se cae antes de borrarlo y lo vuelve a procesar
- **THEN** las ventanas de los minutos afectados conservan la cantidad de muestras del primer procesamiento

### Requirement: Reporte de ventanas a la aplicación
El puente SHALL reportar a la aplicación la ventana de cada minuto que recibió muestras nuevas desde su último reporte, siempre con el total acumulado de ese minuto, porque la aplicación reemplaza una ventana ya registrada en lugar de sumarla. El puente SHALL identificarse con una fuente y una instancia que no cambian entre reinicios, SHALL declarar los límites de bucket usados y SHALL autenticarse con el token de servicio del pipeline. Un reporte SHALL NOT llevar más de 1440 ventanas.

#### Scenario: Muestras tardías en un minuto ya reportado
- **WHEN** el puente reportó la ventana de las 14:05 y después se persisten más muestras de ese minuto
- **THEN** el puente vuelve a reportar la ventana de las 14:05 con el total, y la aplicación queda con ese total

#### Scenario: Reinicio del puente
- **WHEN** el puente se reinicia y vuelve a reportar un minuto que ya había reportado
- **THEN** la aplicación tiene una sola ventana para ese minuto, con las mismas muestras

#### Scenario: Pipeline con tráfico y sin anomalías
- **WHEN** los collectors envían logs durante una hora y el modelo no marca ninguno
- **THEN** la aplicación recibe ventanas de esa hora y no informa al pipeline como degradado por falta de reportes

### Requirement: Entrega durable de las ventanas
Las ventanas que el puente no pudo reportar porque la aplicación no respondía SHALL entregarse cuando la aplicación vuelva. Una falla al reportar SHALL NOT detener ni demorar el procesamiento de logs.

#### Scenario: Aplicación caída durante media hora
- **WHEN** la aplicación no responde durante 30 minutos con logs entrando y después vuelve
- **THEN** las ventanas de esos 30 minutos se reportan sin huecos
