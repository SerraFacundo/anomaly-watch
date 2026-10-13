# Anomaly Watch

Detección de anomalías en logs para PyMES: los nodos de un cliente mandan sus logs, un modelo los
puntúa y la aplicación web convierte las anomalías en alertas.

## Language

**Nodo**:
Una máquina monitoreada del cliente (por ejemplo `web1`, `auth1`). En la aplicación es un
MonitoredNode.
_Avoid_: host (salvo como el nombre de red del nodo), máquina, servidor

**Collector**:
El agente que corre en un nodo y le envía sus logs a la plataforma: hay uno por nodo. En el
pipeline es el agente FluentBit con su token; en la aplicación es la entidad Collector, que pasa a
conectado solo con un heartbeat.
_Avoid_: agente (como término de la aplicación), forwarder

**Pipeline**:
El servicio central que recibe los logs de todos los collectors, los agrupa por plantilla y se los
pasa al modelo para puntuarlos.
_Avoid_: collector (para el servicio central)

**Anomalía**:
Una línea de log que el modelo puntuó por encima del umbral, con la procedencia del modelo que la
marcó.
_Avoid_: alerta, detección

**Alerta**:
Lo que la aplicación abre al agrupar una o más anomalías de un mismo nodo según las reglas de
detección. Es lo que un operador atiende.
_Avoid_: anomalía, incidente
