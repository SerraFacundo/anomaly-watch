# Demo local con varios nodos

Tres agentes FluentBit (`web1`, `web2`, `auth1`) mandan logs al stack central,
todo en una sola máquina. Sirve de respaldo si las VMs no están listas.

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

## Antes de empezar

- Pesos de v3 en `models/cache/modernbert-ft-v3/` (`config.json`,
  `model.safetensors`, `tokenizer.json`, `tokenizer_config.json`,
  `special_tokens_map.json`). Están en `MyDrive/modernbert-ft-v3/`.
- Podman rootless: nginx publica el 443, así que hace falta
  `sudo sysctl net.ipv4.ip_unprivileged_port_start=80`.
- Fedora o RHEL: `chcon -Rt container_file_t models/cache`.
- `openssl` en el host (para el certificado autofirmado).

## Comandos

```bash
deploy/demo/demo.sh up                  # certificado, tokens y stack completo
deploy/demo/demo.sh watch               # vista que se refresca cada 3 s
deploy/demo/demo.sh attack web1         # fuerza bruta SSH en web1
deploy/demo/demo.sh attack web2 web     # SQLi / path traversal en web2
deploy/demo/demo.sh down                # frena todo, conserva datos y buffers
deploy/demo/demo.sh reset               # borra volúmenes y tokens
```

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
   grande: tres agentes con `last_seen_ago` de pocos segundos, líneas contándose
   por host y casi ninguna anomalía con tráfico normal.
3. **Ataque SSH.** En otra terminal: `deploy/demo/demo.sh attack web1`. En
   segundos aparece en `watch` una anomalía de `web1`, fuente `sshd`,
   `Failed password for invalid user ...`, con score cercano a 1. Cada línea
   queda como fila propia: lo que suena a autenticación saltea la caché y el
   agrupamiento por template (`services/collector/cache.py`), porque cada
   intento con su usuario y su IP es evidencia. El tráfico normal de nginx, en
   cambio, se agrupa y casi no cuesta inferencias.
4. **Ataque web.** `deploy/demo/demo.sh attack web2 web`. Aparecen las URLs de
   SQLi y traversal en `web2`, `nginx.access`. Los otros nodos siguen limpios:
   el sistema sabe de qué máquina vino cada cosa.
5. **Resiliencia (opcional).** `podman compose stop ingest`. En `watch`, el
   `last_seen_ago` empieza a crecer: los agentes no pueden entregar y acumulan en
   disco. Lanzar un ataque mientras ingest está caído:
   `deploy/demo/demo.sh attack auth1`. Esperar unos 30 s y
   `podman compose start ingest`. Los agentes reenvían lo acumulado y el ataque
   aparece igual: no se perdió nada durante la caída.
6. **Cierre.** Límite conocido de v3: detecta ataques y anomalías web, no fallas
   operativas (kernel panic, disco lleno). Próximo paso: los mismos agentes en
   VMs reales y el frontend del equipo sobre estas mismas tablas.
