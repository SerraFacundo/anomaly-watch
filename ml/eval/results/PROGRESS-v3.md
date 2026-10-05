# Informe de avance — modelo v3

**Fecha:** 2026-09-10
**Corpus:** `ml/data/build-local`, manifest `e9de7cc79ed3312bb5760fa9e5ae7742edaeaf0a1de107385abd7d94a24ee657`
**Rama:** `feature/brokerless-log-transport`

## Por qué existe v3

`modernbert-ft` (v1) se entrenó con BGL (LogHub) — logs de supercomputadora. Medido contra tráfico real:
- 21,5% de líneas de systemd journal marcadas como anomalía
- 99,0% de líneas de nginx real marcadas como anomalía
- `Failed password for root` puntuaba **0,0000** (normal)
- Licencia de LogHub: "research or academic work" — no apta para producto comercial

v3 se entrena con corpus propio, permisivo, del dominio correcto.

## Corpus

| fuente | registros | tasa anomalía | licencia |
|---|---|---|---|
| nginx (zanbil.ir, e-commerce real) | 163.156 | 0,74% | CC0 ✔ verificada |
| winlog (evtx-baseline + EVTX-to-MITRE-Attack) | 25.328 | 18,58% | Apache-2.0 + CC0 ✔ verificada |
| weblogs (Zenodo Web Server Log Dataset, org-x/org-y) | 178.251 | 7,33% | MIT registrada, **sin re-verificar** (Zenodo caído al momento de construir) |
| **total** | **366.735** | — | — |

**Split final** (55.012 test, 55.011 validation, 256.712 train), 4,9-5,3% anomalías por split.

### Problemas encontrados y corregidos antes de entrenar

1. **Atajo de EventID.** 77 EventIDs de Windows existían solo en ataques → precisión 0,998 memorizando el número, sin leer contenido. Restringido a los 86 EventIDs presentes en ambas clases → F1 del atajo cae a 0,006.
2. **Atajo de identidad.** Los EVTX de ataque se capturaron en máquinas distintas al baseline benigno → hostname/dominio predecía la etiqueta (precisión 1,000). Campos de identidad enmascarados.
3. **Split roto.** `winlog` y `weblogs` son colecciones concatenadas de períodos no solapados, no una sola línea de tiempo — dividir por posición mandaba todo un lado a un solo split (medido: train 0,61% vs test 25,56% en la primera corrida). Corregido a split estratificado por clase con semilla fija.
4. **Categoría dominante.** 99% de los positivos crudos de `weblogs` eran una sola plantilla (`Failed password for X from Y`, 294.304 repeticiones). Acotado a 10.000 por categoría con muestreo parejo — mismo principio que el cache de templates en producción.
5. **Trampa de `code: NNN`.** Las reglas de etiquetado del dataset de Zenodo están escritas para un timeline de log2timeline/plaso, no para el texto crudo — `"code: 404"` no existe como string en el log real. Traducido a un chequeo real de status HTTP, anclado a la posición exacta (misma clase de bug que el falso 5xx ya corregido en `services/collector/cache.py`).

## Resultado — v3 (Colab T4, medido, no proyectado)

| modelo | P | R | F1 | PR-AUC |
|---|---|---|---|---|
| **modernbert-ft-v3** | 0,983 | 0,989 | **0,986** | 0,999 |
| level-keyword (baseline) | 0,797 | 0,494 | 0,610 | 0,419 |
| template-count (baseline) | 0,070 | 0,777 | 0,129 | 0,090 |

Falsos positivos sobre normales: **0,09%** (47/52.300).

### Recall por categoría (lo que importa más que el F1 agregado)

Generaliza — no es solo memorizar la plantilla dominante:

| categoría | recall | n |
|---|---|---|
| sql_injection_attempt | 100% | 2 |
| xss | 100% | 1 |
| dir_scan (nginx) | 99,4% | 178 |
| cms_probe | 100% | 34 |
| bruteforce_login_server_attempt | 100% | 1.509 |
| ~40 técnicas MITRE más | 92-100% | 1-74 |

**Puntos débiles reales** (n suficiente para confiar en el número):

| categoría | recall | n |
|---|---|---|
| T1574 DLL side-loading | 40% | 5 |
| T1202 Indirect Command Execution | 70% | 27 |
| path traversal (nginx) | 80% | 10 |
| T1562.001 Impair Defenses | 84% | 19 |

Con `n=1-2` (file_inclusion, remote_code) no se puede concluir nada — estadísticamente insuficiente.

## Lo que este número NO prueba

Las etiquetas son por regla/substring (propias para nginx, del dataset para weblogs). Que el modelo reconozca bien estos patrones prueba que generaliza **dentro del vocabulario de ataque visto en entrenamiento**, no que vaya a reconocer un ataque con fraseo nuevo que ninguna regla cubrió. Eso solo lo responde tráfico real de un piloto.

## Pendiente (no bloqueante, en curso)

- [ ] **v2 vs v3 en el mismo split** — corriendo en background en esta máquina ahora mismo (CPU, ~30 min). v2 nunca se había evaluado formalmente con harness contra ningún split hasta esta corrida.
- [ ] **Smoke test de v3** (`Accepted publickey` — el falso positivo conocido de v2 puntuando 0,81) — pendiente de bajar el checkpoint (7 archivos raíz, sin `checkpoints/`) o correrlo en Colab.
- [ ] **Registro y promoción** — v3 no está en `ml/models.yaml`, no pasó por `ml/promote.py`. El registry sigue apuntando a `modernbert-ft` (v1), el stack desplegado sigue corriendo `baseline-bert`.
- [ ] **Licencia de Zenodo** — campo MIT registrado pero no re-verificado por caída del sitio. Decisión del usuario, no bloqueante técnico.

## Referencia — smoke test v2 (el fallo que motivó v3)

```
Accepted publickey for deploy           →  0,8131  ANOMALÍA  (falso positivo)
Failed password for invalid user admin  →  0,9970  ANOMALÍA  (correcto)
Failed password for invalid user root   →  0,9979  ANOMALÍA  (correcto)
Kernel panic - not syncing              →  0,0768  normal    (falso negativo — sin logs de kernel Linux en el corpus)
```
