# Mapa: demo integrada (pipeline + aplicación web)

<!-- wayfinder:map — este archivo es el mapa del esfuerzo. Los tickets viven en tasks.md. -->

## Destination

La demo de 10 minutos corre de punta a punta en la laptop con un solo `deploy/demo/demo.sh up`:
cinco nodos con collectors FluentBit entregan logs, el pipeline los puntúa con `modernbert-ft-v3`, y la
aplicación web del equipo muestra en vivo los nodos conectados, las alertas que abren las
anomalías y su evidencia. Se cierra con una corrida ensayada, registrada y con video de respaldo.

## Notes

- **Override de ejecución.** Este mapa no solo decide: también construye. Los tickets de tipo
  `task` entregan código, y el change se archiva cuando el ensayo del ticket "Ensayo general y
  video de respaldo" sale bien. Se ejecuta ticket a ticket con `opsx:apply`.
- **Tracker.** Markdown local: cada ticket es una sección de `tasks.md` con su tipo, quién lo tomó
  (`Tomado por`) y de qué depende (`Bloqueado por`). Un ticket está libre si `Tomado por` es `—`.
  Un ticket se cierra marcando su casilla y escribiendo su `Resolución` abajo; después se agrega
  una línea en "Decisions so far".
- **Dos repos.** Este repo (`SerraFacundo/anomaly-watch`) tiene el mapa, el pipeline y la
  orquestación. La app es `blauerwolf/Anomaly-Watch`; los cambios ahí van en la rama
  `claude/tender-euler-p7i7zs` con PR. La demo corre desde esa rama aunque el PR no esté fusionado.
- **Vocabulario.** Ver `CONTEXT.md` en la raíz: nodo, collector (uno por nodo = agente FluentBit),
  pipeline (el servicio central, aunque su directorio siga llamándose `services/collector/`),
  anomalía, alerta.
- **Skills.** Para tickets `grilling`, usar `grilling` y `domain-modeling`. Para código,
  `ponytail`: lo mínimo que funcione, quedan días.
- **Plataforma.** Podman rootless con SELinux enforcing (Bazzite). Todo bind mount lleva `z`. Ver
  `deploy/demo/RUN-RECORD.md`.

## Decisions so far

- [Destino y alcance de la demo integrada](tasks.md#0-destino-y-alcance-de-la-demo-integrada): todo
  en la laptop con un solo comando; 5 nodos (3 sanos, 2 con problemas); base de la app limpia y
  precalentada por el pipeline real; los nodos se ven online por el heartbeat de su collector; el
  límite del modelo con un kernel panic se muestra en vivo.

## Not yet specified

- **Cuándo un nodo pasa a offline.** El collector se considera desconectado tras 5 minutos sin
  heartbeat (`Collector.HeartbeatGracePeriod`). Si el nodo hereda ese plazo, un kernel panic en vivo
  tarda 5 minutos en notarse: la mitad de la demo. Queda decidir si se acorta para la demo, si el
  nodo deriva su estado al leerlo, o si el guion lo esquiva. Se especifica cuando exista el cambio
  de backend y se sepa cómo se ve en pantalla.
- **Reglas de detección.** Qué reglas se cargan (por ejemplo "Failed password" con severidad High)
  y con qué técnica MITRE, para que la alerta del ataque en vivo destaque sobre las de fondo.
  Depende de qué marca v3 y del guion.
- **Agrupamiento.** El pipeline agrupa por plantilla y hora y solo suma `occurrences` a una fila que
  ya existe. Falta ver si la app necesita enterarse de esas ocurrencias extra o si con la primera
  fila alcanza para la alerta. Se aclara al construir el puente.
- **Ficha de los nodos en la app.** Tipo, entorno, IP, sistema operativo y nombre visible de cada
  uno de los 5 nodos. Es un detalle de presentación y se decide en el guion.
- **Qué pantallas se muestran y en qué orden.** Sale del guion.

## Out of scope

- **Deploy fuera de la laptop** (nube o VMs para el pipeline, la app y los nodos): es el próximo
  esfuerzo, una vez que la demo local esté garantizada.
- **Página de Logs en vivo de la app.** El pipeline descarta las líneas normales después de
  puntuarlas; llevarlas a la app es otro trabajo.
- **Mejorar el modelo** (SQLi, XSS, fallas operativas). La demo muestra lo que v3 hace y lo que no.
- **Modo `Showcase` de la app.** `[DisabledInShowcase]` apaga el endpoint del pipeline.
- **Renombrar `services/collector/`.** Solo cambia el vocabulario en los documentos nuevos.
