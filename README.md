# The Android — guía rápida

## Archivos
- `bot.py` interfaz de Telegram, menús, recargas, avisos y tareas programadas
- `motor.py` fuentes de datos, proyección, picks, récord y calibración
- `db.py` base SQLite (usuarios, créditos, números, picks, recargas, métricas)

## Puesta en marcha
1. **Revoca y crea de nuevo** el token (BotFather → /revoke), la clave de StatPal y la de OnlineSim.
2. `cp .env.example .env` y completa los valores.
3. `pip install -r requirements.txt` y `python bot.py`.
   Al primer arranque importa solo `usuarios_db.json` y `picks_db.json` si existen (los renombra a `.migrado`).
4. Verifica con `/diag` (fuentes, tablas de posiciones, calibración).

## 24/7
- Docker: `docker build -t android . && docker run -d --restart always --env-file .env -v android_data:/data android`
- VPS: usa `bot.service` (reinicio automático).

## Canal público
Crea el canal, agrega el bot como administrador y pon `CANAL_ID` en `.env`. A las 08:00 publica los picks y a las 22:30 el récord completo.

## Horarios automáticos (hora Lima)
Cada 30 min: recalcula picks y liquida resultados. 08:00 aviso de picks. 22:30 récord al canal.

## Comandos de usuario
- `/ganar` qué necesita cada selección de tus partidos pendientes para ganar (en vivo o antes de empezar)
- `/historial` tus últimas consultas con su resultado final
- `/valor <cuota> <prob%>` compara con la cuota real de la casa
- `/record` récord real del bot

## Comandos de admin
- `/lineas` ver las líneas que muestra el bot antes del partido
- `/lineas corners 8.5 9.5 10.5 11.5` fijar las líneas de un mercado (goles, corners, tarjetas); `/lineas reset` restablece
- `/dar`, `/broadcast`, `/diag`, `/metricas`
