# Registro de la primera corrida real

La demo de `deploy/demo/` se escribió y se revisó estáticamente en una sesión en
la nube que no podía bajar imágenes de contenedores. Esto documenta la primera
vez que se ejecutó de verdad, el 12 de octubre de 2026.

El README de esta carpeta dice cómo correrla; `tasks.md` del cambio
`brokerless-log-transport` dice qué tareas cierra. Acá quedan los números y los
cuatro defectos que la ejecución encontró y la revisión estática no.

## Entorno

| | |
|---|---|
| Host | Bazzite, SELinux enforcing |
| Contenedores | Podman 5.8.7, podman-compose 1.6.0 como proveedor externo |
| Modelo | `modernbert-ft-v3`, 575 MB, `models/cache/modernbert-ft-v3/` |
| Detector | CPU, `DETECTOR_NUM_THREADS=2` |

`net.ipv4.ip_unprivileged_port_start` ya estaba en 80, así que el `sysctl` con
sudo que pedía el README no fue necesario. Fedora 41 y posteriores ya traen ese
valor.

## Qué se verificó

Los nueve contenedores levantaron sanos; el detector cargó el modelo en 24 s,
bien por debajo del `start_period` de 90 s del healthcheck.

| paso del guion | resultado |
|---|---|
| 2. Tráfico normal | 3 agentes registrados, 200+ líneas por host, 0 anomalías |
| 3. Fuerza bruta SSH en `web1` | 40 filas, score 1.0000, una fila por intento |
| 4. Escaneo web en `web2` | 7 filas score 1.0000, `web1` y `auth1` limpios |
| 5. Resiliencia | 80 de 80 líneas entregadas, cero pérdida |

El paso 3 confirma el bypass de `services/collector/cache.py`: cada intento de
autenticación es su propia fila (`occurrences = 1`), porque el usuario y la IP
de cada intento son evidencia y no se pueden agrupar por template.

El paso 5, en detalle, porque es el que sostiene la decisión de no usar broker:
línea de base de 40 filas en `auth1`, `podman compose stop ingest`, inyección de
40 líneas de ataque con el stack ciego, 100 s de caída, `start`. Durante la
caída los agentes registran `failed to flush chunk … retry in 8 seconds` y
después `retry in 141 seconds`, conservando los chunks en disco. Al volver,
las filas subieron 40 → 50 → 60 → 80 siguiendo el backoff de FluentBit, y se
quedaron en 80 durante cinco muestras de 45 s.

## Latencia del detector

Medida con `tools/detector_latency.py` adentro del contenedor, que es lo que
pedía el comentario de `ml/models.yaml`:

| métrica | valor |
|---|---|
| p50 una línea | 109.8 ms |
| p95 una línea | 153.7 ms |
| p50 lote de 32 | 2958.5 ms |
| p95 lote de 32 | 3106.4 ms |

El gate de promoción pide p95/32 por debajo de 2.000 ms. Colab había medido
27.196 ms y se sospechaba que era su CPU: la sospecha era correcta, el número
real es 8,75 veces mejor. Pero **sigue sin pasar el gate**, por 1,55x. La
promoción de v3 queda como override explícito, ahora contra una medición.

Lo que lo hace usable no es el modelo sino la caché: 32 líneas en 2.959 ms son
10,8 líneas/s de inferencia cruda, y con 98% de aciertos eso da unas 540
líneas/s efectivas contra un pico de 50 a 200.

Nota para quien toque el dimensionamiento: `services/collector/cache.py` dice
29,6 registros/s, medido en otra máquina. Acá son 10,8. El cálculo de margen de
ese docstring es 2,7 veces optimista.

## Los cuatro defectos

En orden creciente de cuánto importan fuera de la demo.

### 1. El overlay no mezclaba en podman-compose

```
ValueError: can't merge value of [networks] of type <class 'list'> and <class 'dict'>
```

El compose base le daba a nginx `networks: [anomaly-watch]` (lista) y el overlay
necesitaba la forma de mapa para agregarle el alias `anomaly-watch.local`.
`docker compose` normaliza las dos formas antes de mezclar; podman-compose
mezcla por tipo y aborta. nginx usa la forma de mapa en el base.

Solo afecta a la demo, y solo con podman-compose.

### 2. SELinux bloqueaba todos los bind mounts

```
avc: denied { read } for comm="python" name="agent_token.py"
  scontext=...:container_t:s0:c229,c483
  tcontext=...:object_r:user_home_t:s0
```

Ningún bind mount llevaba la opción `z`, así que `container_t` no podía leer
`user_home_t` y la emisión de tokens moría con `[Errno 13] Permission denied`.
Los ocho mounts la llevan ahora, lo que además retira el paso `chcon` que el
README pedía: era parchear uno de ocho síntomas.

Afecta a todo el stack en cualquier host con SELinux, no solo a la demo.

### 3. Los escaneos web nunca se puntuaban

40 líneas de ataque inyectadas en `web2` produjeron 0 inferencias y 0 filas.

En formato combinado todas las líneas de nginx tienen el mismo esqueleto, y
Drain3 enmascara justo lo que varía: la ruta y el status. Con similitud 0.4,
`GET /../../../../etc/passwd" 403` cae en el mismo clúster que
`GET /products" 200`, y la caché le responde a la sonda con el veredicto del
tráfico normal. La línea nunca llega al detector.

`needs_bypass` no lo atajaba porque sus patrones de HTTP cubrían solo 5xx, y los
escaneos son 400, 403 y 404. Ahora 4xx saltea la caché al lado de la regla de
5xx que ya estaba.

Esto revierte una aserción deliberada de `tools/smoke_collector.py` que decía que
un 404 no debía saltear. Esa comprobación se reescribió para conservar lo que de
verdad protegía —que un cuerpo de 512 bytes no se lea como status 5xx— y el
costo quedó acotado: con 93% de aciertos quedan 423 líneas/s efectivas.

### 4. Cualquier reinicio de ingest rompía el pipeline para siempre

```
connect() failed (113: Host is unreachable) while connecting to upstream,
upstream: "http://10.89.3.17:8004/ingest"
```

Un bloque `upstream` en nginx open source resuelve sus nombres una sola vez, al
cargar la config, y guarda la dirección por toda la vida del worker. Podman le da
IP nueva a un contenedor que reinicia, así que después de `start ingest` todos
los lotes recibían 502 hasta recargar nginx a mano.

El arreglo es una variable en `proxy_pass`, que fuerza resolución por request.
La dirección del resolver no está escrita en la config: aardvark-dns responde en
el gateway de la red, que cambia si la red se recrea, así que
`20-resolver-from-resolv-conf.sh` la deriva del `/etc/resolv.conf` del propio
contenedor al arrancar. El costo es el pool de keepalive hacia ingest: nginx
abre una conexión TCP por lote, en un salto de HTTP plano sobre la red interna.

`resolver local=on` leería el `resolv.conf` sin el script, pero nginx 1.31.6 lo
rechaza como hostname.

Este no era un bug de la demo. Convertía un reinicio de 30 segundos en una caída
que lo sobrevivía, y habría roto la garantía de la tarea 9.5 en producción.

## Qué queda abierto

- **Tarea 9.6**, el límite de buffer más allá de 512 MB: sin probar. Haría falta
  sostener una caída larga o bajar el límite a propósito.
- **Tarea 10.1**, FluentBit en una máquina que no sea esta. Los agentes de la
  demo son contenedores en el host de desarrollo, así que nada ejercitó la
  unidad systemd de la tarea 9.4, una red que se parta independientemente del
  stack, ni un reloj que no sea el del stack.
- **El gate de latencia**, descrito arriba.
- **SQLi y XSS**: v3 no los detecta. El corpus tiene escaneos de rutas, no
  inyección. La tabla de scores por payload está en el paso 4 del README.
