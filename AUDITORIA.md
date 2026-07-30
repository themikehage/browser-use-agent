# Auditoría profesional — browser-agent

**Fecha:** 2026-07-30  
**Estado observado en prod:** `running:healthy`, jobs reales completados (Wikipedia/CR7), SQLite con historial, screenshots en `/data`.  
**Stack actual:** FastAPI + browser-use + Xvfb/noVNC (proxy `/vnc`) + SQLite + UI monofile.

---

## 1. Resumen ejecutivo

La base ya es un **MVP usable y estable**: jobs async, SSE, historial persistente, VNC same-origin y cierre de browser.  
El salto a producto “profesional” no es reescribir el core del agente, sino cerrar **seguridad, control operativo, fiabilidad del runtime X11/VNC, observabilidad y pulido de UX**.

| Área | Madurez hoy | Techo con mejoras top |
|------|-------------|------------------------|
| Ejecución de tareas | Alta | Alta |
| Visibilidad / progreso | Media-alta | Alta |
| Seguridad | Baja | Alta |
| Multi-usuario / API | Baja | Media-alta |
| Operación / recovery | Media | Alta |
| UX profesional | Media | Alta |

**ROI máximo (orden recomendado):**  
Auth → cola real + cancel duro → health de VNC/X11 → volumen persistente Coolify → métricas/coste → plantillas UX → tests smoke.

---

## 2. Matriz de mejoras (valor × esfuerzo)

Escala: **Valor** 1–5 (impacto en producto/riesgo), **Esfuerzo** 1–5 (días-persona aprox.), **ROI** = Valor/Esfuerzo.

### P0 — Hacer ya (alto riesgo o valor inmediato)

| # | Mejora | Valor | Esf. | ROI | Por qué |
|---|--------|------:|-----:|----:|---------|
| 1 | **Autenticación de la UI/API** (Basic Auth Coolify o token `Authorization` + cookie de sesión simple) | 5 | 2 | 2.5 | Hoy cualquiera con la URL lanza agentes = **gasto OpenRouter + abuso del browser**. VNC sin password multiplica el riesgo. |
| 2 | **VNC view-only por defecto + password opcional** (`x11vnc -viewonly` / `-passwdfile`) | 5 | 1 | 5.0 | El iframe embebido permite control remoto del desktop; un visitante puede interferir con el agente o navegar a sitios peligrosos. |
| 3 | **Cancelación real del job** (cancelar `asyncio.Task` del run + kill browser inmediato) | 4 | 2 | 2.0 | `request_cancel` solo pone un flag leído en `on_step_end`; entre steps (LLM lento) no para. Usuario cree que canceló y sigue pagando tokens. |
| 4 | **Volumen persistente en Coolify para `/data`** | 5 | 1 | 5.0 | Sin volume, cada redeploy borra historial y screenshots. Local ya tiene volume; prod probablemente no. |
| 5 | **Healthcheck profundo** (`/health` verifica Xvfb + websockify + disk free + db writable) | 4 | 1 | 4.0 | Hoy `/health` solo dice “uvicorn vive”. VNC/X11 pueden estar muertos y la UI “ok”. |

### P1 — Alto valor profesional (siguiente sprint)

| # | Mejora | Valor | Esf. | ROI | Por qué |
|---|--------|------:|-----:|----:|---------|
| 6 | **Cola con N jobs** (FIFO, no 409 al estar busy) | 4 | 2 | 2.0 | UX profesional: encolar varias tareas y ver posición. El runner ya es single-worker; falta aceptar `queued` múltiples. |
| 7 | **Supervisor de procesos** (s6/supervisord) para Xvfb/x11vnc/websockify con restart | 4 | 2 | 2.0 | Si x11vnc muere, el contenedor sigue healthy y el iframe queda negro para siempre. |
| 8 | **Reutilizar browser session** entre jobs (`keep_alive`) o pool de 1 browser caliente | 4 | 3 | 1.3 | Cada job paga 5–15s de cold start Chromium. Mejora latencia percibida y estabilidad del display VNC. |
| 9 | **Coste y métricas por job** (tokens in/out si el history lo expone, duración, steps, modelo) | 4 | 2 | 2.0 | Sin esto no hay control de gasto ni debugging de “¿por qué salió caro?”. |
| 10 | **Rate limit + tamaño de cola + max concurrent=1 explícito** | 4 | 1 | 4.0 | Protege API key y CPU aunque haya auth débil. |
| 11 | **SSE más fiable en el frontend** (backoff, `Last-Event-ID`/`after`, reconexión sin duplicar log) | 3 | 2 | 1.5 | Recargas y cortes de red pueden dejar el log a medias o duplicado. |
| 12 | **Screenshot por step versionado** (`{job_id}/{step}.jpg`) + timeline clickable | 3 | 2 | 1.5 | Hoy se sobrescribe un solo JPG; se pierde la historia visual del run. |
| 13 | **Headers de seguridad** (CSP que permita iframe same-origin VNC, `X-Frame-Options` selectivo, no-sniff, HSTS en proxy) | 3 | 1 | 3.0 | Higiene profesional barata. |
| 14 | **Pins exactos de deps** + lockfile (`uv.lock`/`requirements.lock`) | 3 | 1 | 3.0 | `browser-use` rompe APIs entre minors; rebuilds no reproducibles. |

### P2 — Producto / DX (diferenciación)

| # | Mejora | Valor | Esf. | ROI | Por qué |
|---|--------|------:|-----:|----:|---------|
| 15 | **Plantillas de tareas** (“QA login”, “extraer tabla”, “rellenar form”) | 3 | 1 | 3.0 | Reduce fricción; parece producto, no demo. |
| 16 | **Selector de modelo en UI** (lista corta allowlist OpenRouter) | 3 | 1 | 3.0 | Operar sin redeploy; útil para comparar calidad/coste. |
| 17 | **Export job** (JSON/Markdown del resultado + events + URL final) | 3 | 1 | 3.0 | Compartir resultados y archivar fuera del contenedor. |
| 18 | **Retry con un click** (re-encolar mismo prompt) | 3 | 1 | 3.0 | Flujo natural tras fallos transitorios. |
| 19 | **Allowed/prohibited domains** por job o global | 4 | 2 | 2.0 | Seguridad y compliance; browser-use ya soporta `allowed_domains`. |
| 20 | **Modo headless opcional** (sin VNC, solo screenshots) para jobs baratos/batch | 3 | 2 | 1.5 | Ahorra CPU/RAM cuando no hace falta mirar. |
| 21 | **Webhooks al terminar** (`JOB_WEBHOOK_URL`) | 3 | 1 | 3.0 | Integración con n8n/Slack sin polling. |
| 22 | **API key de servicio** distinta de la UI (machine-to-machine) | 3 | 2 | 1.5 | Automatización externa profesional. |
| 23 | **UI: progreso % / step x de max**, ETA burda, toasts, empty states mejores | 3 | 2 | 1.5 | Percepción de calidad. |
| 24 | **i18n / copy errors humanizados** (mapear errores OpenRouter/Playwright) | 2 | 2 | 1.0 | Menos “Error: Exception…” críptico. |

### P3 — Escala / arquitectura (solo si crece el uso)

| # | Mejora | Valor | Esf. | ROI | Cuándo |
|---|--------|------:|-----:|----:|--------|
| 25 | Workers con browsers remotos (Browser Use Cloud / CDP remoto) | 5 | 5 | 1.0 | Cuando 1 Chromium local no basta |
| 26 | Postgres en lugar de SQLite | 3 | 3 | 1.0 | Multi-réplica o backups gestionados |
| 27 | Multi-tenant + cuotas | 4 | 5 | 0.8 | Si hay varios usuarios/orgs |
| 28 | Grabación de video del run (`browser-use[video]`) | 2 | 2 | 1.0 | Demo/compliance, pesado en disco |
| 29 | Frontend en framework (React/Vue) | 2 | 4 | 0.5 | Solo si la UI crece mucho; monofile aún justifica |
| 30 | OpenTelemetry + logs estructurados JSON a Coolify | 3 | 2 | 1.5 | Cuando debug en prod sea frecuente |

---

## 3. Hallazgos técnicos concretos (deuda actual)

### Seguridad
- **Sin auth** en `/api/tasks` y `/vnc/*`.
- **x11vnc `-nopw`** y proxy que expone control total del display.
- **API key solo en server** (bien), pero el endpoint es el ataque.
- Errores pueden filtrar stack/internal messages al cliente (`str(exc)`).

### Fiabilidad del agente
- Cancel = soft flag; no aborta `agent.run` a mitad de un LLM call.
- Browser nuevo por job → flaky startup + VNC a veces “escritorio vacío” hasta que Chromium pinta.
- Prune de jobs borra filas pero **no borra screenshots huérfanos** en disco.
- `DATA_DIR` default `./data` en código vs `/data` en Docker: frágil si alguien olvida env.
- Jobs `running` al crash → marked failed (bien); **no hay resume**.

### Observabilidad
- Logs texto plano; no hay `job_id` correlacionado de forma sistemática en todos los logs.
- No hay contadores: success rate, duración p50/p95, tokens, errores OpenRouter.
- `/health` no distingue “API up / browser stack down”.

### Frontend
- Un solo HTML grande: OK a esta escala, pero sin tests ni componentes.
- VNC path hardcodeado en parte; si websockify path falla, fallback a screenshot no es automático al error del iframe.
- Sin “copiar resultado”, sin buscar en historial, sin filtros por status.
- `alert()` para errores → poco profesional.

### Ops / Coolify
- `shm_size` y memory limits ya aplicados (bien).
- Falta confirmar **persistent storage** mount `/data`.
- Un solo worker (correcto para Playwright+display); documentar que **no** escalar réplicas sin sticky/session browser remoto.
- Sin smoke test post-deploy automatizado.

### Calidad de código
- Proxy VNC HTTP crea `httpx.AsyncClient` por request (mejor client global/lifespan).
- `import json` dentro de funciones SSE (menor).
- Sin tests unitarios del `JobStore` ni del contract API.
- Sin `ruff`/`mypy` en CI.

---

## 4. Roadmap sugerido por valor acumulado

### Semana 1 — “No me roben ni me quemen la API” (P0)
1. Auth (Coolify basic o bearer token env `APP_AUTH_TOKEN`)
2. VNC view-only (+ pass opcional)
3. Volume `/data` en Coolify
4. Health profundo + rate limit
5. Cancel duro (task.cancel + browser.kill)

**Resultado:** seguro para URL semi-pública, ops predecible.

### Semana 2 — “Se siente producto” (P1 core)
6. Cola N jobs  
7. Métricas/coste por job en UI  
8. Screenshots por step + timeline  
9. Supervisor X11/VNC  
10. Pins/lockfile + smoke test CI  

**Resultado:** usable en el día a día, debuggable, menos sorpresas en deploy.

### Semana 3 — “Integrable” (P2 selectivo)
11. Webhooks + export  
12. Plantillas + retry + model picker  
13. `allowed_domains`  
14. Headers seguridad + errores humanizados  

**Resultado:** encaja en flujos (n8n, equipos) sin reescritura.

### Más adelante (solo con demanda)
- Browser remoto / multi-sesión  
- Multi-tenant  
- Frontend framework  

---

## 5. Quick wins (< 2 h cada uno)

| Quick win | Impacto |
|-----------|---------|
| `x11vnc -viewonly` | Seguridad inmediata |
| Env `APP_AUTH_TOKEN` middleware FastAPI | Cierra API abierta |
| Borrar screenshots en `_prune` | Disco no crece sin control |
| Client `httpx` global para proxy VNC | Menos leaks/conexiones |
| Mostrar duración `finished_at - started_at` en lista jobs | UX gratis |
| Botón “Copiar resultado” | DX |
| `GET /health` → `{xvfb, vnc, disk_free_mb}` | Ops |
| Allowlist de modelos en backend | Evita typos caros |
| Documentar “1 réplica max” en about.md | Evita mal deploy |
| Test smoke: create job mock / health / vnc 200 | Confianza en redeploy |

---

## 6. Anti-mejoras (no priorizar ahora)

- Reescribir en microservicios  
- Kubernetes  
- React “porque sí”  
- Postgres sin multi-instancia  
- Parallel browsers en el mismo Xvfb (dolor de ventanas/foco)  
- Auth OAuth complejo si solo hay 1–3 usuarios (basta token/basic)

---

## 7. Criterios de “profesional y robusto” (definition of done)

La app se puede llamar robusta/profesional cuando:

- [ ] No es usable sin credencial  
- [ ] VNC no permite control no autorizado  
- [ ] Historial y screenshots sobreviven redeploy  
- [ ] Cancel detiene gasto de tokens en < 5s  
- [ ] `/health` refleja stack real (API+X11+VNC+disk)  
- [ ] Cada job muestra duración, steps, error claro o resultado exportable  
- [ ] Cola acepta trabajo sin “busy 409” frustrante  
- [ ] Redeploy tiene smoke test automático  
- [ ] Deps reproducibles (lockfile)  
- [ ] Un fallo de x11vnc se auto-recupera o marca degraded  

---

## 8. Recomendación final

**No hace falta un rewrite.** El diseño jobs+SSE+SQLite+VNC proxy es el correcto.

Invertir el 80% del esfuerzo en:

1. **Seguridad de acceso** (auth + VNC view-only)  
2. **Control de ejecución** (cancel real, cola, rate limit, coste)  
3. **Runtime display** (supervisor, health, browser keep-alive)  
4. **Evidencia del run** (screenshots por step, export, métricas)  

Eso multiplica la confianza y el valor percibido mucho más que features cosméticas o escalado prematuro.

---

## 9. Scorecard actual (subjetivo)

| Dimensión | Nota /10 | Comentario |
|-----------|--------:|------------|
| Funcionalidad agente | 8 | Ya completa tareas reales |
| Arquitectura jobs | 8 | Sólida para single-node |
| Seguridad | 2 | URL abierta + VNC control |
| Observabilidad | 4 | Logs básicos, sin costes |
| UX | 6 | Buena base, faltan detalles pro |
| Operación/deploy | 6 | Healthy, falta volume/supervisor |
| Mantenibilidad | 6 | Módulos claros, sin tests |
| **Global** | **5.5** | MVP fuerte; prod-pro con P0+P1 → ~8 |
