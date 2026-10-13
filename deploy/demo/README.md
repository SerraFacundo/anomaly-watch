# Demo local con varios nodos

Cinco agentes FluentBit (`web1`, `web2`, `auth1`, `db1`, `mail1`) mandan logs al
stack central, y la aplicación web del equipo muestra lo que encuentra, todo en
una sola máquina. `db1` y `mail1` tienen problemas de baja intensidad de fondo;
los otros tres están sanos.

```
pipeline (postgres: anomalies, agents) ──bridge──> app: POST /api/v1/pipeline/anomalies
                                                    app: POST /api/v1/collectors/{code}/heartbeat
```

El puente (`services/bridge/bridge.py`) es lo que conecta las dos mitades: manda
cada anomalía nueva a la app, que abre las alertas, y traduce "este agente
entregó logs" en el heartbeat de su collector, que pone el nodo online.

```
loggen ──escribe──> /logs/<nodo>/*.log ──tail──> agent-<nodo> (FluentBit, buffer en disco)
                                                   │ HTTPS + token
                                                   v
                         nginx ─> ingest ─> spool ─> collector ─> detector ─> postgres
```

Cada agente es el mismo contenedor que iría en una VM: lee archivos con `tail`,
guarda en disco (`storage.type filesystem`) y envía por TLS con su propio token.
El generador escribe tráfico normal de nginx y sshd, y bajo pedido inyecta un
ataque.

Los números medidos en la primera corrida real, y los cuatro defectos que esa
corrida encontró, están en [RUN-RECORD.md](RUN-RECORD.md).

## Antes de empezar

- Pesos de v3: `demo.sh up` los baja solo a `models/cache/modernbert-ft-v3/`
  desde el repo privado de Hugging Face `fsaecr/modernbert-ft-v3`, fijado al
  commit `e1d3703d` (otro repo o revisión: `MODEL_REPO=usuario/repo`,
  `MODEL_REVISION=<sha>`). Necesita `uv` y un token de lectura de HF:
  `export HF_TOKEN=hf_...` o `uvx --from huggingface_hub hf auth login` una vez.
  Son 575 MB: `config.json`, `model.safetensors`, `tokenizer.json`,
  `tokenizer_config.json`, `special_tokens_map.json` y `run_metadata.json`.
- Podman rootless: nginx publica el 443, así que hace falta
  `sudo sysctl net.ipv4.ip_unprivileged_port_start=80`. Fedora 41 y posteriores
  ya vienen con ese valor; comprobalo con
  `sysctl net.ipv4.ip_unprivileged_port_start` antes de pedir sudo.
- `openssl` en el host (para el certificado autofirmado).

No hace falta ningún `chcon`: los bind mounts llevan la opción `z` y Podman
reetiqueta solo. Sin ella, un host con SELinux en enforcing deniega que
`container_t` lea `user_home_t` y el proceso del contenedor muere con
`[Errno 13] Permission denied`.

## La aplicación web

`demo.sh up` también levanta la app desde un checkout hermano (`APP_DIR`, por
defecto `../Anomaly-Watch`), en la rama que tenga los cambios de la demo: el
heartbeat que pone los nodos online y `Seed:Skip`. Corre como proyecto `aw-app`,
aparte de cualquier stack de la app levantado a mano, pero usa los mismos
puertos (8080, 3000, 5432 y 6379): frenar ese otro stack antes.

- Si la app no tiene `.env`, se crea desde `.env.example`. `PIPELINE_SERVICE_TOKEN`
  sale de ahí y se le pasa al puente.
- El seed omite los nodos, logs, alertas y topología ficticios. Usuarios, roles y
  reglas sí se siembran, incluida la regla "SSH Brute Force" (5 fallos en 5
  minutos, Critical, T1110), que es la que dispara el ataque en vivo.
- `tools/app_setup.py` crea un collector y un nodo por host y registra el modelo.
  Las credenciales de los collectors quedan en `deploy/demo/app-credentials.env`
  (modo 600, fuera de git).
- Entrar en http://localhost:3000 con `admin@anomalywatch.local` / `password123`.

## Comandos

```bash
deploy/demo/demo.sh up                  # certificado, tokens, app y stack completo
deploy/demo/demo.sh watch               # vista del pipeline que se refresca cada 3 s
deploy/demo/demo.sh attack web1         # fuerza bruta SSH en web1
deploy/demo/demo.sh attack web2 web     # SQLi / path traversal en web2
deploy/demo/demo.sh attack db1 panic    # kernel panic en db1: v3 no lo marca
deploy/demo/demo.sh warmup              # otra tanda de problemas de fondo en db1 y mail1
deploy/demo/demo.sh down                # frena los dos stacks, conserva datos y buffers
deploy/demo/demo.sh reset               # borra volúmenes, tokens y credenciales de los dos
```

Los logs del puente dicen qué mandó y qué respondió la app:
`podman compose -f docker-compose.yml -f deploy/demo/compose.demo.yml logs -f bridge`.

Con Docker en vez de Podman: `COMPOSE="docker compose" deploy/demo/demo.sh up`.

Los tokens quedan en `deploy/demo/tokens.env` (modo 600, fuera de git). Si se
borra la base sin pasar por `reset`, los tokens viejos dejan de valer: correr
`reset` y después `up`.

La primera vez el detector tarda en cargar el modelo (healthcheck con
`start_period` de 90 s); el collector espera a que esté listo.

## Guion (unos 10 minutos)

1. **Arquitectura (2 min).** Mostrar el diagrama de arriba. Puntos clave: no hay
   broker; la cola es el buffer en disco de cada agente más el spool durable entre
   ingest y collector. Ingest responde 202 recién cuando el lote está en disco.
2. **Arranque.** Tener el stack ya levantado con `up` antes de la exposición
   (la carga del modelo no es material de demo). Abrir `watch` en una terminal
   grande: tres agentes presentes, líneas contándose por host y casi ninguna
   anomalía con tráfico normal.

   `last_seen_ago` va a oscilar entre 0 y 60 s aunque todo esté sano: el
   `touch()` de `services/ingest/auth.py` está limitado a una escritura por
   minuto por agente, para que un agente que entrega cada segundo no sea una
   escritura por segundo sobre la misma fila. Lo que importa es que vuelva a
   cero, no el valor instantáneo.
3. **Ataque SSH.** En otra terminal: `deploy/demo/demo.sh attack web1`. En
   segundos aparece en `watch` una anomalía de `web1`, fuente `sshd`,
   `Failed password for invalid user ...`, con score cercano a 1. Cada línea
   queda como fila propia: lo que suena a autenticación saltea la caché y el
   agrupamiento por template (`services/collector/cache.py`), porque cada
   intento con su usuario y su IP es evidencia. El tráfico normal de nginx, en
   cambio, se agrupa y casi no cuesta inferencias.
4. **Ataque web.** `deploy/demo/demo.sh attack web2 web`. Aparecen URLs de
   escaneo en `web2`, `nginx.access`, con score 1.0. Los otros nodos siguen
   limpios: el sistema sabe de qué máquina vino cada cosa.

   De los 8 payloads que sortea `loggen`, v3 marca 2: el traversal a
   `/etc/passwd` y el sondeo de `/wp-login.php`. Medido contra el checkpoint,
   línea de nginx completa en formato combinado:

   | payload | score |
   |---|---|
   | `/../../../../etc/passwd` | 1.0000 |
   | `/wp-login.php` | 1.0000 |
   | `/index.php?page=php://filter/...` | 0.1129 |
   | `/.env` | 0.0117 |
   | `/cgi-bin/.%2e/.%2e/bin/sh` | 0.0028 |
   | `/search?q=<script>alert(...)</script>` | 0.0013 |
   | `/login?user=admin'--` | 0.0009 |
   | `/products?id=1' OR '1'='1` | 0.0000 |

   Así que de 40 líneas inyectadas se encienden unas 7 a 10, no 40. SQLi y XSS
   se le escapan: el corpus de v3 tiene escaneos de rutas, no inyección. El
   generador queda con los 8 a propósito, para que en la demo se vea lo que el
   modelo hace y lo que no.
5. **Resiliencia (opcional).** `podman compose stop ingest`. En `watch`, el
   `last_seen_ago` pasa de 60 s y sigue subiendo sin volver a cero: los agentes
   no pueden entregar y acumulan en disco. Lanzar un ataque mientras ingest está
   caído: `deploy/demo/demo.sh attack auth1`. Esperar a que el contador supere
   los 90 s —menos que eso es indistinguible del ciclo normal del `touch()`— y
   `podman compose start ingest`. Los agentes reenvían lo acumulado y el ataque
   aparece igual: no se perdió nada durante la caída.
6. **Cierre.** Límite conocido de v3: detecta ataques y anomalías web, no fallas
   operativas (kernel panic, disco lleno). Próximo paso: los mismos agentes en
   VMs reales y el frontend del equipo sobre estas mismas tablas.
