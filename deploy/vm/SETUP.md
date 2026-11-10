# Desplegar el stack de ML en una VM

Levanta `nginx` (TLS) → `ingest` → Redis → `collector` → `detector` → Postgres en
una VM, con imágenes publicadas y [docker-compose.hub.yml](docker-compose.hub.yml).
Las anomalías salen por el stream de Redis `anomaly_watch:alerts`, y la API de la
web app las lee desde ahí. El repo no hace falta en la VM.

Cada paso termina en **Listo cuando**: no sigas hasta cumplirlo.

## Reglas de seguridad

- `HF_TOKEN` vive solo en la sesión. No va en archivos ni en `.env`.
- Los tokens de agente se muestran una sola vez. Guardalos con modo `600`.
- `REDIS_BIND` y `DETECTOR_BIND` son la IP privada, nunca `0.0.0.0`: `/score` no
  tiene autenticación. El NSG cierra `6379` y `8003` a internet.
- Los agentes verifican el certificado con `tls.ca_file`. `tls.verify` queda en `On`.

## 1. Publicar las imágenes

En la máquina que construye, desde la raíz del repo y con `docker login` hecho.
La VM es x86: sin `--platform`, una imagen arm64 muere con `exec format error`.

```bash
for pair in detector:detector ingest:ingest collector:collector; do
  docker buildx build --platform linux/amd64 -f services/${pair%%:*}/Dockerfile \
    -t ivan75/anomaly-watch:${pair##*:} --push .
done
```

**Listo cuando:** Docker Hub muestra los tres tags. Para otro repo, definí
`DETECTOR_IMAGE`, `INGEST_IMAGE` y `COLLECTOR_IMAGE` en `.env`.

## 2. Preparar la carpeta

En la VM, copiá estos archivos desde el repo a una carpeta de despliegue, con las
mismas rutas relativas:

```
docker-compose.yml                      (deploy/vm/docker-compose.hub.yml)
deploy/nginx/nginx.conf
deploy/nginx/20-resolver-from-resolv-conf.sh
tools/agent_token.py
```

Creá `.env` con modo `600`. `PRIV_IP` es la IP privada de la VM:

```bash
REDIS_BIND=<PRIV_IP>
DETECTOR_BIND=<PRIV_IP>
POSTGRES_PASSWORD=<clave generada>
DETECTOR_NUM_THREADS=2
```

**Listo cuando:** `docker compose config -q` no devuelve error.

## 3. Bajar los pesos

```bash
read -rs HF_TOKEN && export HF_TOKEN
mkdir -p models/cache/modernbert-ft-v3
docker run --rm -e HF_TOKEN -v "$PWD/models/cache/modernbert-ft-v3:/out" \
  docker.io/library/python:3.12-slim sh -c '
    pip install -q huggingface_hub &&
    hf download fsaecr/modernbert-ft-v3 \
      --revision e1d3703d90af061aae46c40d4d01425679a6e7b2 --local-dir /out \
      --exclude ".gitattributes" --exclude "checkpoints/*" --exclude "*.bin" --exclude "*.pt"'
unset HF_TOKEN
```

**Listo cuando:** la carpeta tiene `model.safetensors` (unos 598 MB), `config.json`
y `tokenizer.json`.

## 4. Generar el certificado

Los nodos validan contra este certificado. Tiene que incluir el nombre con el que
se conectan.

```bash
mkdir -p deploy/nginx/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout deploy/nginx/certs/server.key -out deploy/nginx/certs/server.crt \
  -subj "/CN=anomaly-watch.local" \
  -addext "subjectAltName=DNS:anomaly-watch.local,IP:<PRIV_IP>,IP:127.0.0.1"
chmod 600 deploy/nginx/certs/server.key
```

**Listo cuando:** `openssl x509 -in deploy/nginx/certs/server.crt -noout -ext subjectAltName`
muestra `<PRIV_IP>`.

## 5. Levantar

```bash
docker compose pull
docker compose up -d --no-build
docker compose ps
```

El detector tarda hasta 90 s en cargar el modelo.

**Listo cuando:** los seis servicios están `healthy` o, en `nginx`, `running`.

## 6. Conectar la web app

En el `.env` de la web app, y después `docker compose up -d --no-deps api`:

```bash
MODEL_STREAM_ENABLED=true
MODEL_STREAM_REDIS_URL=redis://<PRIV_IP>:6379/0
ML_DETECTOR_URL=http://<PRIV_IP>:8003
```

**Listo cuando:** el log de la API muestra `Iniciando consumidor de stream` sin
`Fallo al leer`, y la card "ML detector" figura sana.

## 7. Dar de alta un nodo

El nombre de host tiene que ser idéntico en los dos lados. Si no, la API rechaza
sus anomalías.

1. En la web app: **Configuración → Recolectores → Agregar recolector**, y después
   **Nodos → Agregar nodo**, con **Nombre de host** igual a `<HOST>`.
2. En la VM, emití el token:

   ```bash
   docker compose run --rm -T ingest sh -c \
     'python tools/agent_token.py --dsn "$ANOMALY_WATCH_DSN" issue --host <HOST>'
   ```

3. En el nodo, instalá el agente según [deploy/fluentbit/README.md](../fluentbit/README.md).

Si el nodo está fuera de la VNet, llega por la IP pública con el nombre del
certificado (`--add-host anomaly-watch.local:<IP_PUBLICA>` en un contenedor), y el
NSG tiene que permitirle el `443`.

**Listo cuando:** `agent_token.py list` muestra el host como `active`, y un
intento de login fallido real abre una alerta en la web app.

### Heartbeat de los nodos

Para que la web app marque como desconectado al nodo que deja de mandar logs, el
`collector` le reporta cada minuto cuándo habló cada host. Se configura una sola
vez, con el recolector del punto 1. El nodo tiene que tener ese recolector.

En el `.env` de la VM, y después `docker compose up -d --no-deps collector`:

```bash
ANOMALYWATCH_API_URL=https://<url de la API de la web app>
COLLECTOR_CODE=<código del recolector>
COLLECTOR_TOKEN=<credencial del recolector, la que muestra la web app al crearlo>
```

```bash
docker compose exec collector python -c \
  'import json,urllib.request; print(json.load(urllib.request.urlopen("http://localhost:8005/status"))["node_heartbeat"])'
```

**Listo cuando:** eso muestra `enabled: True`, un `last_sent_at` reciente y
`rejected` vacío, y el nodo figura conectado en la web app.

## 8. Comprobar el reinicio

```bash
sudo reboot
```

**Listo cuando:** sin acción manual, `docker compose ps` vuelve al estado del paso 5.
