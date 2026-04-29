# Guia de contribucion

## Flujo de trabajo

1. Tomar un issue del Kanban board
2. Crear una rama desde `main`
3. Hacer los cambios
4. Abrir un Pull Request hacia `main`
5. Esperar 1 aprobacion y que el CI pase
6. Mergear

## Nombrado de ramas
```
<tipo>/<descripcion-corta>
feat/deteccion-anomalias-logbert
fix/error-endpoint-logs
docs/acta-de-constitucion
chore/configurar-docker
```

## Conventional Commits

Cada commit debe seguir este formato:

```
<tipo>(<scope>): <descripcion corta en minusculas>
```
### Tipos

| Tipo | Cuando usarlo |
|---|---|
| `feat` | Nueva funcionalidad |
| `fix` | Correccion de bug |
| `docs` | Solo documentacion |
| `chore` | Mantenimiento, configs |
| `refactor` | Cambio sin nueva funcionalidad |
| `test` | Agregar o modificar tests |
| `ci` | Cambios en CI/CD |

### Scopes

| Scope | Componente |
|---|---|
| `backend` | API y logica de servidor |
| `frontend` | Interfaz web |
| `ml` | Pipeline de machine learning |
| `docs` | Documentacion academica |
| `ci` | Configuracion de CI/CD |

### Ejemplos
feat(ml): agregar modelo de deteccion LogBERT
fix(backend): corregir error en endpoint de logs
docs(pmi): actualizar acta de constitucion
chore(ci): agregar job de lint para frontend

## Pull Requests

- El titulo del PR debe seguir el mismo formato que los commits
- Referenciar siempre el issue relacionado con `Closes #N`
- El PR no se mergea sin al menos 1 aprobacion
- El CI debe pasar antes de mergear

## Reglas generales

- No pushear directamente a `main`
- No commitear archivos `.env`, datos o modelos entrenados
- Mantener el Kanban actualizado al mover tareas