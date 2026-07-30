# Análisis profesional: qué falta para que funcione correctamente

## Resumen ejecutivo

La arquitectura es correcta (FastAPI + browser-use + Xvfb/VNC en un contenedor), pero hay **fallos críticos de integración API, ciclo de vida del browser, Docker y UX** que impiden un funcionamiento fiable en producción.

---

## 1. Críticos (bloquean o rompen la ejecución)

### 1.1 LLM: API incorrecta para OpenRouter

Usar `ChatOpenAI` + `base_url` puede fallar en tool calling / structured output. `browser-use` expone **`ChatOpenRouter`** nativo.

### 1.2 Browser no se cierra → fugas de procesos/memoria

Tras `agent.run()` no hay cierre del browser. Cada tarea deja Chromium huérfano → OOM.

### 1.3 Race condition en el lock de tareas

El check de `current_task_running` estaba fuera del lock.

### 1.4 Sin manejo de errores → 500 opaco

Falta try/except, validación de API key al arranque, timeout y `max_steps`.

### 1.5 Chromium en Docker sin flags necesarios

Falta `shm_size`, `chromium_sandbox=False`, `--disable-dev-shm-usage`.

### 1.6 `start.sh` frágil

Sin process supervision, healthchecks ni manejo de señales.

---

## 2. Altos (funcionan a medias / mala UX en prod)

### 2.1 Frontend incompleto

Sin manejo de HTTP errors, botón no se deshabilita, VNC URL hardcodeada a `:6080`.

### 2.2 Endpoint síncrono de larga duración

`POST /task` bloquea minutos; proxies cortan. Ideal: jobs async (mejora futura).

### 2.3 Dependencias sin pin

`browser-use` cambia API entre minors.

### 2.4 Modelo por defecto

Preferir modelos con buen structured output (Sonnet / equivalentes).

---

## 3. Checklist de “mínimo viable correcto”

### Código (`app.py`)
- [x] LLM vía OpenRouter (`ChatOpenAI` + base_url, API soportada por browser-use)
- [x] Validar env al startup
- [x] Lock atómico (check + set dentro del lock)
- [x] `try/except/finally` con cierre del browser
- [x] `max_steps` + timeout
- [x] `GET /health`
- [x] Logs de error

### Docker
- [x] `shm_size: 2gb`
- [x] `chromium_sandbox=False` / `--disable-dev-shm-usage`
- [x] Pins en requirements
- [x] start.sh más robusto
- [x] `HEALTHCHECK`

### Frontend
- [x] Manejo de HTTP errors (409/500)
- [x] Botón disabled + estado
- [x] URL VNC configurable (`VNC_PUBLIC_URL`)

### Ops
- [x] `.env.example`
- [x] about.md actualizado
- [ ] Redesploy Coolify
- [ ] Probar tarea end-to-end

---

## 4. Severidad por impacto

```
P0  Browser sin close + sin shm_size     → OOM / Chromium crash
P0  LLM wrapper incorrecto / API key     → tarea falla al primer step
P0  Lock race + sin except               → dobles browsers / 500 mudos
P1  POST síncrono + timeouts proxy       → “funciona en local, falla en Coolify”
P1  VNC URL y -nopw                      → no se ve / inseguro
P2  Deps sin pin, start.sh frágil        → deploys no reproducibles
P2  Frontend mínimo                      → mala DX, difícil debug
```

## 5. Conclusión

Con los fixes P0/P1 implementados el MVP queda estable. Mejoras futuras: jobs asíncronos, supervisor de procesos (supervisord), VNC autenticado/proxied.
