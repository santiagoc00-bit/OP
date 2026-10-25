# Asistente de soporte con IA (WhatsApp + Telegram)

Bot de atención al cliente para un negocio: responde preguntas frecuentes con la
información real del negocio y, cuando no sabe o el cliente lo pide, **transfiere
la conversación a una persona**.

Misma lógica para los dos canales:

| Canal | Estado | Cómo se usa |
|---|---|---|
| **Telegram** | Listo | **Modo polling: se prueba ya, sin URL pública ni ngrok** |
| **WhatsApp Cloud API** | Listo | Canal oficial de Meta para producción |

- **IA:** Google Gemini (salida estructurada: respuesta + decisión de escalar)
- **Backend:** Python 3.11+ / FastAPI
- **Persistencia:** SQLite (historial, estado y deduplicación)

---

## Probar con Telegram en 2 minutos

### 1. Crear el bot

1. Abre Telegram y busca **@BotFather**.
2. Envía `/newbot`, elige un nombre y un usuario que termine en `bot`
   (ej. `cafeluna_soporte_bot`).
3. Copia el **token** que te devuelve (`123456789:AAH...`).

### 2. Configurar

```powershell
cd D:\H\OP
Copy-Item .env.example .env
notepad .env
```

Solo necesitas dos valores para empezar:

```env
TELEGRAM_BOT_TOKEN=123456789:AAH...
GEMINI_API_KEY=AIza...
TELEGRAM_MODE=polling
BUSINESS_NAME=Café Luna
ADMIN_KEY=una-clave-larga
```

> Con `TELEGRAM_MODE=polling` **no hace falta ngrok ni dominio**: el servidor
> pregunta a Telegram si hay mensajes nuevos.

### 3. Arrancar

```powershell
.\run.cmd
```

> Si PowerShell te bloquea `run.ps1` ("running scripts is disabled on this
> system"), usa `run.cmd`, que no depende de la política de ejecución. Como
> alternativa puntual: `powershell -ExecutionPolicy Bypass -File .\run.ps1`.

En el log verás:

```
INFO  telegram: Telegram conectado como @cafeluna_soporte_bot
INFO  asistente: Telegram en modo polling (no necesita URL pública).
INFO  asistente: Asistente listo. Negocio: Café Luna
```

Abre tu bot en Telegram → **Start** → escribe `¿hacen envíos a Zapopan?`.

### 4. Comandos útiles en Telegram

| Comando | Qué hace |
|---|---|
| `/start` | Saludo inicial y reactiva el bot si estaba pausado |
| `/menu` o `/menu@TuBot` | Devuelve el control al bot tras un escalado |

También puedes escribir `menú`, `bot` o `reiniciar` en texto normal.

### Pasar Telegram a modo webhook (producción)

El polling es ideal para desarrollo, pero mantiene una conexión abierta. En un
servidor con HTTPS, usa webhook:

```env
TELEGRAM_MODE=webhook
TELEGRAM_WEBHOOK_SECRET=una-frase-larga
```

Y registra la URL una sola vez:

```powershell
$token = "123456789:AAH..."
$url = "https://tudominio.com/telegram/webhook"
$secret = "una-frase-larga"
Invoke-RestMethod "https://api.telegram.org/bot$token/setWebhook?url=$url&secret_token=$secret"
```

> Polling y webhook son excluyentes. Al arrancar en modo polling el bot borra el
> webhook automáticamente (si no, Telegram devuelve error 409).

---

## Cómo funciona

```
WhatsApp ──┐
           ├──► mensaje normalizado ──► dedupe + SQLite
Telegram ──┘                              │
                                          ▼
                                   espera 2 s (agrupa mensajes)
                                          │
                                          ▼
                        Gemini + knowledge/negocio.md
                                          │
                        ┌─────────────────┴─────────────────┐
                        │ handoff = false                   │ handoff = true
                        ▼                                   ▼
                  responde al cliente          responde + pausa el bot
                                               + avisa al equipo
```

Cada conversación se identifica con una **clave**:

- WhatsApp: el teléfono → `5215512345678`
- Telegram: `tg:<chat_id>` → `tg:123456789`

Esa clave es la que usas en el panel de administración, sin importar el canal.

Decisiones de diseño:

- **El bot no inventa.** Su única fuente de verdad es `knowledge/negocio.md`.
- **El escalado es determinista.** Gemini devuelve un JSON con `handoff: true/false`
  según reglas explícitas, no hay que interpretar el texto.
- **Idempotente.** Meta y Telegram reintentan; se deduplica por id de mensaje.
- **Agrupa mensajes.** Si el cliente escribe "hola" / "buenas" / "una duda", el bot
  responde una sola vez con todo el contexto (`BUFFER_SECONDS`).
- **Humano al mando.** Al escalar, la conversación queda pausada: el bot se calla
  hasta que alguien escriba `/menu` o la reanudes desde el panel.
- **Formato por canal.** El mismo texto se envía como `*negrita*` en WhatsApp y
  como HTML escapado en Telegram.
- **Respuesta instantánea.** Los webhooks contestan `200` al instante.

---

## WhatsApp (producción)

1. Entra a [developers.facebook.com](https://developers.facebook.com/) → **My Apps**
   → **Create App** → tipo **Business**.
2. Añade el producto **WhatsApp** → **Set up**. Meta te da un número de prueba gratis.
3. En **WhatsApp → API Setup** copia el **token** y el **Phone number ID**.
4. En **App settings → Basic** copia el **App Secret**.
5. Expón el servidor: `ngrok http 8000` (o tu dominio en producción).
6. En **WhatsApp → Configuration**:
   - **Callback URL:** `https://tu-url/webhook`
   - **Verify token:** el mismo `WA_VERIFY_TOKEN` del `.env`
   - **Webhook fields:** suscribe **messages**
7. Completa el `.env`:

```env
WA_TOKEN=EAA...
WA_PHONE_NUMBER_ID=1234567890
WA_VERIFY_TOKEN=inventa-una-frase-larga
WA_APP_SECRET=abc123...
```

Los dos canales pueden funcionar a la vez.

---

## Personalizar el bot (lo más importante)

Edita **`knowledge/negocio.md`**. Ahí va todo: horarios, precios, envíos, pagos,
políticas, preguntas frecuentes y lo que **no** hace el negocio.

- Se recarga automáticamente: no hace falta reiniciar.
- Si un dato no está en el archivo, el bot escala a un humano en vez de inventarlo.
- Cuanto más concreto y en lenguaje natural esté escrito, mejor responde.
- Ajusta el tono y las reglas del prompt en `app/ai.py` (`SYSTEM_PROMPT`).

### Probar sin enviar mensajes

```powershell
$body = @{ text = "¿Hacen envíos a Zapopan?" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8000/admin/simulate `
  -Headers @{ "X-Admin-Key" = "TU_ADMIN_KEY" } `
  -ContentType "application/json; charset=utf-8" -Body $body
```

Devuelve qué diría el bot, si escalaría y por qué, y cómo se vería en cada canal.

---

## Panel de administración

Todo requiere la cabecera `X-Admin-Key`.

| Método | Ruta | Para qué |
|---|---|---|
| `GET` | `/admin/conversations?paused=true&channel=telegram` | Ver conversaciones (y cuáles esperan a un humano) |
| `GET` | `/admin/conversations/{clave}` | Historial completo de un cliente |
| `POST` | `/admin/conversations/{clave}/pause` | Silenciar el bot en esa conversación |
| `POST` | `/admin/conversations/{clave}/resume` | Devolverle el control al bot |
| `POST` | `/admin/send` | Escribir como humano: `{"to":"tg:123456789","text":"..."}` |
| `POST` | `/admin/simulate` | Probar el bot sin enviar nada |
| `GET` | `/admin/knowledge` | Estado de la base de conocimiento |

La `{clave}` es el teléfono (WhatsApp) o `tg:<id>` (Telegram).

Cuando el bot escala, el cliente ve el mensaje de traspaso y la conversación queda
`paused`. Una persona la retoma desde `/admin/send` y, al terminar, la reanuda
(o el cliente escribe `/menu`).

### Aviso de escalado

Si configuras `HANDOFF_WEBHOOK_URL`, cada escalado envía un JSON a esa URL:

```json
{ "business": "Café Luna", "conversation": "tg:123456789", "reason": "reclamo por pedido incompleto" }
```

Sirve para un canal de Slack/Telegram/Make/n8n y que el equipo se entere al momento.

---

## Si el bot responde "estoy teniendo un problema técnico"

Ese texto significa que el mensaje **sí llegó** y el bot intentó contestar, pero la
llamada a Gemini falló. Ejecuta:

```powershell
.\check.cmd
```

El diagnóstico prueba la configuración, el token de Telegram, la clave de Gemini y
te dice qué modelo puedes usar. Interpretación:

| Salida | Qué hacer |
|---|---|
| `[FALLA] El token de Telegram no funciona` | Regenera el token en @BotFather y actualiza `.env` |
| `[FALLA] No se pudieron listar los modelos` | La clave de Gemini es inválida o de otro proyecto |
| `[AVISO] 'gemini-2.5-flash' no está en la lista` | Modelo retirado: copia uno de la lista a `GEMINI_MODEL` |
| `[AVISO] Sigue siendo el ejemplo de la cafetería` | Edita `knowledge/negocio.md` |

La terminal donde corre `run.cmd` también muestra la causa exacta en una línea
`Gemini fallo (modelo '...'): ...`.

> Un `GET /v1/models 404` en el log **no** viene de este proyecto: es otro programa
> llamando a nuestro puerto. El bot habla con Gemini por HTTPS, no con ese endpoint.

> `run.cmd` vigila también el `.env`: al guardarlo, el servidor se reinicia solo y
> toma los valores nuevos. Si arrancaste `uvicorn` a mano sin
> `--reload-include ".env"`, reinícialo tras cambiar credenciales.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Cubren firma HMAC, verificación del webhook de WhatsApp, secreto del webhook de
Telegram, polling, deduplicación en ambos canales, parsing de payloads (texto,
botones, multimedia, comandos), formato por canal y aislamiento de conversaciones.

---

## Desplegar en producción

1. **Servidor:** cualquier host con Python. Recomendados: Railway, Render, Fly.io
   o un VPS con Docker. Necesita HTTPS y una URL fija.
2. **Variables:** configura todas las del `.env` en el panel del host (nunca subas `.env`).
3. **Telegram:** cambia a `TELEGRAM_MODE=webhook`, define `TELEGRAM_WEBHOOK_SECRET`
   y registra la URL (ver arriba).
4. **WhatsApp:** cambia la Callback URL de Meta a `https://tudominio.com/webhook`.
5. **Seguridad:** `WA_APP_SECRET`, `TELEGRAM_WEBHOOK_SECRET` y `ADMIN_KEY` obligatorios.
6. **Token de WhatsApp permanente:** crea un *System User* en Business Settings con
   permiso `whatsapp_business_messaging`.
7. **SQLite:** sirve para una PyME, pero necesita disco persistente. Si el host es
   efímero, monta un volumen o cambia a Postgres.
8. **Un solo proceso:** el agrupador de mensajes vive en memoria; con varios
   workers (`--workers 2+`) cada uno tendría su propio buffer. Usa 1 worker o mueve
   el buffer a Redis.

### Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `GEMINI_API_KEY` | sí | API key de Google AI Studio |
| `TELEGRAM_BOT_TOKEN` | un canal | Token de @BotFather |
| `TELEGRAM_MODE` | no | `polling` (defecto), `webhook` u `off` |
| `TELEGRAM_WEBHOOK_SECRET` | producción | Valida el webhook de Telegram |
| `WA_TOKEN` | un canal | Token de acceso de Meta |
| `WA_PHONE_NUMBER_ID` | un canal | ID del número (no el teléfono) |
| `WA_VERIFY_TOKEN` | con WhatsApp | Secreto del handshake de Meta |
| `WA_APP_SECRET` | producción | Valida la firma HMAC de los webhooks |
| `GEMINI_MODEL` | no | Por defecto `gemini-2.5-flash` |
| `BUSINESS_NAME` | no | Nombre que usa el prompt |
| `KNOWLEDGE_PATH` | no | Por defecto `knowledge/negocio.md` |
| `BUFFER_SECONDS` | no | Agrupación de mensajes (0 = desactivado) |
| `HISTORY_TURNS` | no | Mensajes de contexto enviados al modelo |
| `HANDOFF_MESSAGE` | no | Texto de traspaso a humano |
| `HANDOFF_WEBHOOK_URL` | no | Aviso de escalado al equipo |
| `ADMIN_KEY` | sí | Clave del panel de administración |
| `TIMEZONE` | no | Zona horaria para fechas ("¿abren hoy?") |

---

## Límites y buenas prácticas

**WhatsApp**

- **Ventana de 24 h.** Puedes responder libremente dentro de las 24 h siguientes
  al último mensaje del cliente. Fuera de esa ventana solo puedes iniciar con
  **plantillas aprobadas** por Meta. Para soporte reactivo casi nunca es problema.
- **Opt-in.** El cliente debe haberte escrito primero o haber aceptado recibir mensajes.
  No uses el bot para publicidad masiva: es la vía rápida al bloqueo.

**Telegram**

- El bot solo atiende **chats privados** (los grupos se ignoran para no responder
  a todo el mundo).
- Límite de ~30 mensajes/segundo por bot; sobra de sobra para soporte.

**General**

- **Nada de datos sensibles.** El prompt prohíbe pedir contraseñas, tarjetas o
  documentos. Para pagos, manda al cliente a un enlace oficial.
- **Sé transparente.** Si preguntan si es un bot, debe decirlo.

## Costos aproximados

| Concepto | Costo |
|---|---|
| Telegram Bot API | Gratis |
| WhatsApp Cloud API | Tramo gratuito mensual; después ~$0.01–0.08 USD por conversación según país |
| Gemini 2.5 Flash | ~$0.30 USD por millón de tokens de entrada, ~$2.50 por millón de salida |
| Hosting | $0 (free tier) a ~$7 USD/mes |

1.000 conversaciones al mes suelen quedar por debajo de $10 USD. Con Telegram, $0 de canal.

---

## Estructura

```
app/
  main.py       Rutas FastAPI: webhooks, admin y orquestación
  ai.py         Prompt, cliente de Gemini y salida estructurada
  payload.py    Modelo común de mensajes + parsing de WhatsApp
  telegram.py   Cliente de Telegram, parsing de updates y polling
  whatsapp.py   Envío de mensajes por Cloud API
  db.py         SQLite: historial, pausas, canal y deduplicación
  buffer.py     Agrupación de mensajes seguidos
  format.py     Markdown → WhatsApp / HTML de Telegram
  security.py   Firma HMAC de Meta
  knowledge.py  Carga de la base de conocimiento
knowledge/
  negocio.md    ⬅️ TU INFORMACIÓN VA AQUÍ
tests/
  test_app.py
```

## Siguientes pasos posibles

- Pedidos: tool calling para armar carrito y guardarlo en la base.
- Citas: integración con Google Calendar.
- RAG: si la información crece mucho, trocear el Markdown y usar embeddings.
- Multi-negocio: una tabla de negocios y selección por `phone_number_id` / bot.
- Panel web: una UI sencilla sobre `/admin/conversations` para el equipo.
