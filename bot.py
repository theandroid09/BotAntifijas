"""The Android — bot de Telegram (interfaz). La lógica de análisis está en motor.py y los datos en db.py."""
import asyncio
import logging
import math
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, time as dtime

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db as bd
import motor
from motor import SEP, _corto, _limpio

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx imprime la URL con el token

# ==================== CONFIGURACIÓN (todo por variables de entorno / .env) ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ONLINESIM_API_KEY = os.getenv("ONLINESIM_API_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CANAL_ID = os.getenv("CANAL_ID", "")                  # ej. @mi_canal o -1001234567890 (el bot debe ser admin)
YAPE_NUMERO = os.getenv("YAPE_NUMERO", "")
YAPE_TITULAR = os.getenv("YAPE_TITULAR", "")


def _paquetes():
    out = []
    for trozo in os.getenv("PAQUETES", "5:10,10:22,20:50").split(","):
        try:
            soles, cred = trozo.split(":")
            out.append((float(soles), float(cred)))
        except ValueError:
            pass
    return out


PAQUETES = _paquetes()
COSTO_CONSULTA = 1.0
COSTO_FIJAS = 2.0
COSTOS_NUMERO = {"telegram": 2.0, "whatsapp": 3.0, "google": 2.5, "openai": 3.0}
VENTANA_REFRESH = 600          # segundos de actualización gratis tras consultar un partido en vivo
POR_PAGINA = 8

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logo_path = next((os.path.join(BASE_DIR, f) for f in
                  ["android logo.jpg", "android logo.JPG", "android logo.jpeg", "logo.jpg"]
                  if os.path.exists(os.path.join(BASE_DIR, f))), None)

esperando_entrada = {}          # uid -> "analizar_partido" | ("comprobante", idx_paquete)
_refresh_hasta = {}             # (uid, event_id) -> timestamp
_en_curso = set()               # uids con un análisis pesado en marcha
_ventanas = defaultdict(deque)


# ==================== UTILIDADES ====================
def limitado(uid, clave, maximo, segundos):
    """True si el usuario superó 'maximo' acciones de tipo 'clave' en 'segundos'. El admin no tiene límite."""
    if uid == ADMIN_ID:
        return False
    ahora = time.time()
    q = _ventanas[(uid, clave)]
    while q and ahora - q[0] > segundos:
        q.popleft()
    if len(q) >= maximo:
        return True
    q.append(ahora)
    return False


def kb(*filas):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in fila] for fila in filas])


VOLVER = [("🏠 Menú Principal", "btn_start")]


def _plano(t):
    return t.replace("*", "").replace("`", "").replace("_", "")


async def _edit(msg, texto, teclado=None):
    """Edita un mensaje (texto o foto con caption) con Markdown y, si Telegram lo rechaza, en texto plano."""
    metodo = msg.edit_caption if msg.photo else msg.edit_text
    limite = 1000 if msg.photo else 4000
    for pm, t in (("Markdown", texto[:limite]), (None, _plano(texto)[:limite])):
        try:
            await metodo(t, reply_markup=teclado, parse_mode=pm)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except Exception as e:
            logging.warning(f"edit falló: {e}")
            return


async def _ack(query, texto=None, alerta=False):
    try:
        await query.answer(texto, show_alert=alerta)
    except Exception:
        pass


def _asegurar(user, ref=None):
    return bd.usuario(user.id, user.first_name or "", ref=ref, admin=(user.id == ADMIN_ID))


async def _premiar(context, uid):
    """Si el usuario fue invitado y esta es su primera acción de pago, premia a quien lo invitó."""
    inv = await asyncio.to_thread(bd.premiar_referido, uid)
    if inv:
        try:
            await context.bot.send_message(int(inv), f"🎉 Un amigo que invitaste hizo su primera consulta: "
                                                     f"+{bd.REF_BONUS:g} 🪙 para ti.")
        except Exception:
            pass


# ==================== ONLINESIM ====================
def _sin_clave(texto):
    return str(texto).replace(ONLINESIM_API_KEY, "***") if ONLINESIM_API_KEY else str(texto)


def comprar_numero_onlinesim(service="telegram", country="7"):
    url = "https://onlinesim.io/api/getNum.php"
    try:
        res = requests.get(url, params={"apikey": ONLINESIM_API_KEY, "service": service, "country": country}, timeout=10)
        if res.status_code != 200:
            return None, None, "Error HTTP"
        data = res.json()
        if data.get("response") != 1:
            return None, None, f"Error Code: {data.get('response')}"
        tzid = data.get("tzid")
        phone = "(míralo en Revisar SMS)"
        try:
            st = requests.get("https://onlinesim.io/api/getState.php",
                              params={"apikey": ONLINESIM_API_KEY, "tzid": tzid}, timeout=10)
            if st.status_code == 200:
                j = st.json()
                item = j[0] if isinstance(j, list) else j
                phone = item.get("number") or item.get("phone") or phone
        except Exception:
            pass
        return tzid, phone, "Éxito"
    except Exception as e:
        logging.error(f"OnlineSim compra: {_sin_clave(e)}")
        return None, None, "No se pudo conectar con el proveedor"


def revisar_sms_onlinesim(tzid):
    """Devuelve (texto, lista_sms, codigo_de_estado_del_proveedor)."""
    try:
        res = requests.get("https://onlinesim.io/api/getState.php",
                           params={"apikey": ONLINESIM_API_KEY, "tzid": tzid, "message_to_code": 1}, timeout=10)
        if res.status_code == 200:
            j = res.json()
            item = j[0] if isinstance(j, list) else j
            code, msg, resp = item.get("code", ""), item.get("msg", ""), str(item.get("response", ""))
            if code or msg:
                return "✅ ¡Código SMS recibido! 🚀", [{"sender": "OnlineSim", "text": msg, "code": code}], resp
            return "⏳ Esperando SMS...", [], resp
        return "❌ Error al consultar.", [], ""
    except Exception as e:
        logging.error(f"OnlineSim estado: {_sin_clave(e)}")
        return "❌ Error al consultar.", [], ""


# ==================== MENÚS ====================
def _teclado_principal(user_id):
    filas = [[("🏆 Picks", "menu_picks")],
             [("⚽ Consultar partido", "menu_partido")],
             [("👤 Mi cuenta", "menu_cuenta")],
             [("📱 Servicios (números virtuales)", "btn_menu_servicios")]]
    if user_id == ADMIN_ID:
        filas.append([("🛠️ Panel Admin", "btn_admin_panel")])
    return kb(*filas)


def _texto_principal(user):
    return (f"🤖 *THE ANDROID - PANEL PRINCIPAL* 🤖\n\n👋 Hola, {_limpio(user.first_name)}.\n"
            f"💳 Créditos: `{bd.creditos(user.id)}` 🪙")


async def mostrar_menu_principal(query):
    user = query.from_user
    esperando_entrada.pop(user.id, None)
    _asegurar(user)
    await _edit(query.message, _texto_principal(user), _teclado_principal(user.id))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    esperando_entrada.pop(user.id, None)
    ref = None
    if context.args and context.args[0].startswith("ref_"):
        ref = context.args[0][4:]
    _asegurar(user, ref)
    bd.log_evento(user.id, "start")
    mensaje, teclado = _texto_principal(user), _teclado_principal(user.id)
    if logo_path:
        try:
            with open(logo_path, "rb") as photo:
                await update.message.reply_photo(photo=photo, caption=mensaje, reply_markup=teclado, parse_mode="Markdown")
                return
        except Exception:
            pass
    await update.message.reply_text(mensaje, reply_markup=teclado, parse_mode="Markdown")


async def menu_picks(query):
    await _ack(query)
    await _edit(query.message,
                f"🏆 *PICKS*\nLos que el modelo ve más probables, con cuota justa y nivel de confianza.\n"
                f"Cada pick se guarda y se califica solo: míralo en el récord.",
                kb([(f"🏆 Mejores del día [{COSTO_FIJAS:g} 🪙]", "btn_fijas")],
                   [(f"🇵🇪 Liga 1 Perú [{COSTO_FIJAS:g} 🪙]", "btn_fijas_pe")],
                   [("📈 Récord real del bot", "btn_record")], VOLVER))


async def menu_partido(query):
    await _ack(query)
    esperando_entrada.pop(query.from_user.id, None)
    await _edit(query.message,
                f"⚽ *CONSULTAR PARTIDO*\nAnálisis completo: forma, proyección y combinadas. "
                f"Cuesta `{COSTO_CONSULTA:g}` 🪙 solo si se encuentra el partido.",
                kb([("📅 Partidos de hoy (elige con un toque)", "hoy|0")],
                   [("✍️ Escribir equipos", "manual_match")], VOLVER))


async def menu_cuenta(query):
    await _ack(query)
    await menu_cuenta_silencioso(query)


async def ver_cuenta(query):
    await _ack(query)
    u = _asegurar(query.from_user)
    tot, pag = bd.ref_stats(query.from_user.id)
    await _edit(query.message,
                f"👤 *PERFIL*\n🆔 ID: `{query.from_user.id}`\n💳 Créditos: `{round(u['creditos'], 2)}` 🪙\n"
                f"🛒 Compras: `{u['compras']}`\n🔎 Consultas: `{u['consultas']}`\n👥 Invitados: `{tot}` (activos: `{pag}`)",
                kb([("⬅️ Mi cuenta", "menu_cuenta")], VOLVER))


async def reclamar_bonus(query):
    estado, extra = bd.reclamar_bonus(query.from_user.id)
    if estado == "ok":
        await _ack(query, f"🎉 ¡{bd.BONUS_DIARIO:g} créditos añadidos!", True)
        await menu_cuenta_silencioso(query)
    elif estado == "nuevo":
        await _ack(query, f"🕒 Tu cuenta es nueva: el bonus diario se activa en ~{math.ceil(extra / 3600)} h.", True)
    else:
        await _ack(query, f"⏳ Ya reclamaste tu bonus. Vuelve en ~{math.ceil(extra / 3600)} h.", True)


async def menu_cuenta_silencioso(query):
    u = _asegurar(query.from_user)
    await _edit(query.message, f"👤 *MI CUENTA*\n💳 Créditos: `{round(u['creditos'], 2)}` 🪙",
                kb([("📋 Mi perfil", "btn_cuenta")], [("📜 Mi historial de consultas", "btn_hist")],
                   [("🎁 Bonus diario", "btn_bonus")],
                   [("💵 Recargar (Yape / Plin)", "btn_recarga")], [("👥 Invitar amigos (gana créditos)", "btn_ref")],
                   [(("🔔 Avisos: ACTIVADOS (tocar para apagar)" if u["avisos"] else "🔕 Avisos: apagados (tocar para activar)"),
                     "btn_avisos")], VOLVER))


async def alternar_avisos(query):
    activos = bd.alternar_avisos(query.from_user.id)
    await _ack(query, "🔔 Avisos activados" if activos else "🔕 Avisos apagados")
    await menu_cuenta_silencioso(query)


async def invitar(query, context):
    await _ack(query)
    link = f"https://t.me/{context.bot.username}?start=ref_{query.from_user.id}"
    tot, pag = bd.ref_stats(query.from_user.id)
    await _edit(query.message,
                f"👥 *INVITA Y GANA*\nCuando un amigo entre con tu enlace y haga su primera consulta o compra, "
                f"recibes `{bd.REF_BONUS:g}` 🪙 (hasta {bd.REF_MAX_DIA} premios al día).\n\n"
                f"🔗 Tu enlace:\n`{link}`\n\n👥 Invitados: `{tot}` • que ya usaron el bot: `{pag}`",
                kb([("⬅️ Mi cuenta", "menu_cuenta")], VOLVER))


# ==================== PARTIDOS CON BOTONES ====================
async def mostrar_partidos(query, pagina=0):
    await _ack(query)
    esperando_entrada.pop(query.from_user.id, None)
    try:
        lista = await asyncio.to_thread(motor.listar_partidos)
    except Exception as e:
        logging.error(f"Lista partidos: {e}")
        lista = []
    if not lista:
        await _edit(query.message, "📅 No encontré partidos en vivo ni próximos ahora mismo.\n"
                                   "Puedes escribir los equipos a mano.",
                    kb([("✍️ Escribir equipos", "manual_match")], [("🔄 Reintentar", "hoy|0")], VOLVER))
        return
    paginas = max(1, math.ceil(len(lista) / POR_PAGINA))
    pagina = max(0, min(pagina, paginas - 1))
    hoy = datetime.now(motor.LIMA).date()
    filas = []
    for p in lista[pagina * POR_PAGINA:(pagina + 1) * POR_PAGINA]:
        L, V = _corto(p["local"], 11), _corto(p["visita"], 11)
        if p["estado"] == "live":
            etiqueta = f"🔴 {L} {p['gl'] if p['gl'] is not None else 0}-{p['gv'] if p['gv'] is not None else 0} {V}"
        else:
            dia = "" if p["inicio"].date() == hoy else "mañ "
            etiqueta = f"{'🇵🇪' if p['liga_code'] == 'per.1' else '🕒'} {dia}{p['inicio']:%H:%M} {L} vs {V}"
        filas.append([(etiqueta, f"m|{p['liga_code']}|{p['event_id']}")])
    nav = []
    if pagina > 0:
        nav.append(("⬅️", f"hoy|{pagina - 1}"))
    nav.append((f"🔄 {pagina + 1}/{paginas}", f"hoy|{pagina}"))
    if pagina < paginas - 1:
        nav.append(("➡️", f"hoy|{pagina + 1}"))
    filas += [nav, [("✍️ Escribir equipos", "manual_match")], VOLVER]
    await _edit(query.message,
                f"📅 *PARTIDOS* _(hora Lima)_\n🔴 en vivo primero · 🇵🇪 Liga 1 al inicio · {len(lista)} partidos\n"
                f"Toca uno para analizarlo (`{COSTO_CONSULTA:g}` 🪙).", kb(*filas))


async def solicitar_analisis_manual(query):
    await _ack(query)
    esperando_entrada[query.from_user.id] = "analizar_partido"
    await query.message.reply_text(
        "⚽ *CONSULTA DE PARTIDOS (MODO CONTINUO)*\n\n"
        "Envía un partido (Ej: Alianza Lima vs Universitario).\n"
        "Funciona con partidos en vivo, por empezar y recién terminados.\n"
        f"Solo se cobra `{COSTO_CONSULTA:g}` crédito si se encuentra el partido.\n\n"
        "👇 Puedes enviar varios seguidos:",
        reply_markup=kb([("📅 Mejor elige de la lista", "hoy|0")], VOLVER), parse_mode="Markdown")


async def ejecutar_consulta(context, user, msg, *, texto="", liga=None, event_id=None, gratis=False, modo_texto=False):
    """Analiza un partido y edita 'msg' con el resultado. Cobra solo si el análisis tuvo éxito."""
    uid = user.id
    admin = uid == ADMIN_ID
    sin_saldo = kb([("💵 Recargar", "btn_recarga")], VOLVER)
    if uid in _en_curso:
        await _edit(msg, "⏳ Sigo analizando tu consulta anterior, dame unos segundos.", kb(VOLVER))
        return
    if not gratis and not admin and bd.creditos(uid) < COSTO_CONSULTA:
        await _edit(msg, f"❌ Saldo insuficiente (requiere {COSTO_CONSULTA:g} 🪙).", sin_saldo)
        return
    _en_curso.add(uid)
    try:
        meta = {}
        reporte, error = await asyncio.to_thread(motor.analizar_partido, texto, event_id, liga, meta)
        if error:
            await _edit(msg, error, kb([("📅 Partidos de hoy", "hoy|0")], VOLVER))
            return
        if not gratis and not admin:
            if not bd.cobrar(uid, COSTO_CONSULTA, "consulta"):
                await _edit(msg, f"❌ Saldo insuficiente (requiere {COSTO_CONSULTA:g} 🪙).", sin_saldo)
                return
            await _premiar(context, uid)
        bd.log_evento(uid, "consulta_gratis" if gratis else "consulta")
        if meta.get("event_id") and meta.get("liga") and not gratis:
            bd.consulta_guardar(uid, meta, finalizada=(meta.get("estado") == "finished"))
        vivo = meta.get("estado") == "live" and meta.get("event_id") and meta.get("liga")
        filas = []
        if vivo:
            _refresh_hasta[(uid, str(meta["event_id"]))] = (
                _refresh_hasta.get((uid, str(meta["event_id"])), 0) if gratis else time.time() + VENTANA_REFRESH)
            filas.append([("🔄 Actualizar (gratis por 10 min)", f"ref|{meta['liga']}|{meta['event_id']}")])
        if meta.get("event_id") and meta.get("estado") != "finished":
            filas.append([("🎯 ¿Qué necesito para ganar?", f"gan|{meta['event_id']}")])
        filas += [[("📅 Partidos de hoy", "hoy|0")], VOLVER]
        pie = f"\n\n💳 Créditos: `{bd.creditos(uid)}` 🪙"
        if vivo:
            pie += f"\n🕒 Actualizado `{datetime.now(motor.LIMA):%H:%M:%S}`"
        if modo_texto:
            pie += "\n💬 Envía otro partido para seguir consultando:"
        await _edit(msg, reporte + pie, kb(*filas))
    finally:
        _en_curso.discard(uid)


async def partido_seleccionado(query, context):
    _, liga, event_id = query.data.split("|", 2)
    user = query.from_user
    if limitado(user.id, "pesado", 4, 60):
        await _ack(query, "⏳ Demasiadas consultas seguidas, espera un momento.", True)
        return
    await _ack(query)
    msg = await query.message.reply_text("⏳ Analizando el partido...")
    await ejecutar_consulta(context, user, msg, liga=liga, event_id=event_id)


async def refrescar_partido(query, context):
    _, liga, event_id = query.data.split("|", 2)
    user = query.from_user
    if _refresh_hasta.get((user.id, event_id), 0) < time.time():
        await _ack(query, f"⌛ Terminó tu ventana gratis. Consulta de nuevo el partido ({COSTO_CONSULTA:g} 🪙).", True)
        return
    if limitado(user.id, "refresh", 1, 10):
        await _ack(query, "⏳ Espera unos segundos entre actualizaciones.")
        return
    await _ack(query, "🔄 Actualizando...")
    await ejecutar_consulta(context, user, query.message, liga=liga, event_id=event_id, gratis=True)


async def manejar_texto_usuario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if esperando_entrada.get(user.id) != "analizar_partido":
        return
    _asegurar(user)
    if limitado(user.id, "pesado", 4, 60):
        await update.message.reply_text("⏳ Demasiadas consultas seguidas, espera un momento.")
        return
    msg = await update.message.reply_text("⏳ Buscando el partido y calculando promedios...")
    await ejecutar_consulta(context, user, msg, texto=update.message.text.strip(), modo_texto=True)


# ==================== PICKS Y RÉCORD ====================
async def fijas_handler(query, context):
    user = query.from_user
    pe = query.data == "btn_fijas_pe"
    admin = user.id == ADMIN_ID
    if limitado(user.id, "pesado", 4, 60):
        await _ack(query, "⏳ Demasiadas consultas seguidas, espera un momento.", True)
        return
    await _ack(query)
    _asegurar(user)
    volver = kb([("⬅️ Picks", "menu_picks")], VOLVER)
    if not admin and bd.creditos(user.id) < COSTO_FIJAS:
        await query.message.reply_text(f"❌ Saldo insuficiente (requiere {COSTO_FIJAS:g} 🪙).",
                                       reply_markup=kb([("💵 Recargar", "btn_recarga")], VOLVER))
        return
    msg = await query.message.reply_text("⏳ Cargando picks...")
    res = await asyncio.to_thread(motor.fijas_del_dia, pe)
    if not res:
        await _edit(msg, "⚠️ No hay picks con suficiente respaldo estadístico ahora. "
                         "No se descontó ningún crédito.", volver)
        return
    if not admin:
        if not bd.cobrar(user.id, COSTO_FIJAS, "consulta_picks"):
            await _edit(msg, f"❌ Saldo insuficiente (requiere {COSTO_FIJAS:g} 🪙).", volver)
            return
        await _premiar(context, user.id)
    bd.vistas_registrar(user.id, res["ids"])
    await _edit(msg, res["texto"] + f"\n\n💳 Créditos: `{bd.creditos(user.id)}` 🪙", volver)


async def _enviar_record(msg):
    await asyncio.to_thread(motor.liquidar_pendientes)
    texto = await asyncio.to_thread(motor.record_texto)
    await _edit(msg, texto, kb([("⬅️ Picks", "menu_picks")], VOLVER))


async def record_handler(query):
    await _ack(query)
    msg = await query.message.reply_text("⏳ Actualizando resultados...")
    await _enviar_record(msg)


async def comando_record(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Actualizando resultados...")
    await _enviar_record(msg)


async def comando_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uso = "Uso: `/valor <cuota> <probabilidad%>`\nEj: `/valor 1.85 62`"
    try:
        cuota = float(context.args[0].replace(",", "."))
        prob = float(context.args[1].replace("%", "").replace(",", ".")) / 100
        if cuota <= 1 or not 0 < prob < 1:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(uso, parse_mode="Markdown")
        return
    ev = prob * cuota - 1
    justa = 1 / prob
    veredicto = ("✅ *Hay valor*: la casa paga más de lo que el modelo estima justo." if ev > 0.03 else
                 "⚖️ *Casi justo*: sin ventaja clara." if ev > -0.03 else
                 "❌ *Sin valor*: la casa paga menos de lo justo. Mejor pasa.")
    await update.message.reply_text(
        f"💰 *VALOR DE LA CUOTA*\nCuota: `{cuota:.2f}` | Prob. modelo: `{prob:.0%}`\n"
        f"Cuota justa: `{justa:.2f}`\nVentaja esperada: `{ev:+.1%}`\n\n{veredicto}", parse_mode="Markdown")


# ==================== /GANAR, HISTORIAL Y LÍNEAS ====================
async def _responder_ganar(msg, consultas):
    textos = await asyncio.to_thread(lambda: [motor.ganar_texto(c) for c in consultas])
    await _edit(msg, ("\n" + SEP + "\n").join(textos), kb([("📜 Mi historial", "btn_hist")], VOLVER))


async def comando_ganar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _asegurar(user)
    if limitado(user.id, "ganar", 4, 60):
        await update.message.reply_text("⏳ Espera unos segundos antes de volver a consultar.")
        return
    bd.log_evento(user.id, "ganar")
    pend = bd.consultas_de(user.id, limite=3, solo_pendientes=True)
    if not pend:
        await update.message.reply_text("No tienes partidos pendientes. Consulta uno en *Consultar partido* y luego "
                                        "escribe /ganar para ver qué necesita cada selección.",
                                        parse_mode="Markdown", reply_markup=kb([("⚽ Consultar partido", "menu_partido")]))
        return
    msg = await update.message.reply_text("⏳ Revisando tus partidos...")
    await _responder_ganar(msg, pend)


async def ganar_callback(query):
    event_id = query.data.split("|", 1)[1]
    c = bd.consulta_por_evento(query.from_user.id, event_id)
    if not c:
        await _ack(query, "No encontré esa consulta en tu historial.", True)
        return
    if limitado(query.from_user.id, "ganar", 4, 60):
        await _ack(query, "⏳ Espera unos segundos.", True)
        return
    await _ack(query)
    msg = await query.message.reply_text("⏳ Revisando el partido...")
    await _responder_ganar(msg, [c])


def _historial_texto(uid):
    cs = bd.consultas_de(uid, limite=10)
    if not cs:
        return "📜 *MI HISTORIAL*\nAún no has consultado partidos."
    s = "📜 *MI HISTORIAL* _(últimas 10 consultas)_\n"
    ico = {"ganada": "✅", "perdida": "❌", "nula": "➖"}
    pt = {"segura": "🟢", "equilibrada": "🟡", "arriesgada": "🔴", "pick": "☝️"}
    for c in cs:
        try:
            fecha = f"{datetime.fromisoformat(c['inicio']):%d/%m %H:%M}"
        except Exception:
            fecha = ""
        s += f"\n{{}} *{_corto(c['local'] or '?', 12)}* vs *{_corto(c['visita'] or '?', 12)}* _{fecha}_\n".format(
            "⏳" if c["estado"] == "pendiente" else "🏁")
        r = c["resultado"]
        if c["estado"] == "pendiente":
            s += "   pendiente — escribe /ganar para ver qué necesita\n"
        elif r and r.get("gl") is not None:
            tiers = " ".join(f"{pt.get(t['tipo'], '')}{ico.get(t['estado'], '')}" for t in r["tiers"]) or "sin selecciones"
            s += f"   final `{r['gl']}-{r['gv']}` • {tiers}\n"
        else:
            s += "   sin resultado disponible\n"
    return s


async def historial_callback(query):
    await _ack(query)
    await _edit(query.message, _historial_texto(query.from_user.id), kb([("⬅️ Mi cuenta", "menu_cuenta")], VOLVER))


async def comando_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _asegurar(update.effective_user)
    await update.message.reply_text(_historial_texto(update.effective_user.id), parse_mode="Markdown")


async def comando_lineas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lineas  → ver  |  /lineas corners 8.5 9.5 10.5  → fijar  |  /lineas reset"""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if args and args[0].lower() == "reset":
        motor.guardar_lineas(None, None)
    elif args:
        mercado = {"goles": "goles", "corners": "corners", "esquinas": "corners",
                   "tarjetas": "amarillas", "amarillas": "amarillas"}.get(args[0].lower())
        try:
            vals = [float(x.replace(",", ".")) for x in args[1:]]
            if not mercado or not vals or any(v % 1 != 0.5 or not 0.5 <= v <= 30 for v in vals):
                raise ValueError
        except ValueError:
            await update.message.reply_text("Uso: /lineas <goles|corners|tarjetas> 8.5 9.5 10.5 (solo líneas .5)\n"
                                            "/lineas reset restablece.")
            return
        motor.guardar_lineas(mercado, vals)
    cur = motor.lineas_actuales()
    await update.message.reply_text(
        "📐 Líneas que muestra el bot antes del partido:\n"
        f"Goles: {cur['goles']}\nTiros de esquina: {cur['corners']}\nTarjetas amarillas: {cur['amarillas']}\n"
        "En vivo no se filtran (la casa mueve las líneas).")


# ==================== RECARGAS YAPE / PLIN ====================
async def recarga_menu(query):
    await _ack(query)
    if not PAQUETES or not YAPE_NUMERO:
        await _edit(query.message, "💵 Las recargas aún no están configuradas. Escribe al administrador.",
                    kb([("⬅️ Mi cuenta", "menu_cuenta")], VOLVER))
        return
    filas = [[(f"S/ {s:g} → {c:g} 🪙", f"rc|{i}")] for i, (s, c) in enumerate(PAQUETES)]
    await _edit(query.message,
                f"💵 *RECARGAR CRÉDITOS*\nPaga por *Yape o Plin* al número `{YAPE_NUMERO}`"
                f"{' (' + _limpio(YAPE_TITULAR) + ')' if YAPE_TITULAR else ''} y luego envía aquí la captura.\n\n"
                "Elige tu paquete:", kb(*filas, [("⬅️ Mi cuenta", "menu_cuenta")], VOLVER))


async def recarga_paquete(query):
    idx = int(query.data.split("|")[1])
    if idx >= len(PAQUETES):
        await _ack(query, "Paquete no válido.", True)
        return
    await _ack(query)
    soles, cred = PAQUETES[idx]
    esperando_entrada[query.from_user.id] = ("comprobante", idx)
    await query.message.reply_text(
        f"💵 *Paquete: S/ {soles:g} → {cred:g} 🪙*\n\n1️⃣ Yapea o Plinea *S/ {soles:g}* al `{YAPE_NUMERO}`.\n"
        "2️⃣ Envíame aquí la *captura de pantalla* (como foto).\n"
        "3️⃣ Lo reviso y te acredito en minutos.\n\n⚠️ Cada captura solo sirve una vez.",
        reply_markup=kb([("❌ Cancelar", "btn_start")]), parse_mode="Markdown")


async def manejar_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    estado = esperando_entrada.get(user.id)
    if not (isinstance(estado, tuple) and estado[0] == "comprobante"):
        await update.message.reply_text("Si quieres recargar, entra a *Mi cuenta → Recargar* y elige un paquete primero.",
                                        parse_mode="Markdown")
        return
    if limitado(user.id, "foto", 3, 300):
        await update.message.reply_text("⏳ Demasiados envíos seguidos, intenta en unos minutos.")
        return
    soles, cred = PAQUETES[estado[1]]
    foto = update.message.photo[-1]
    rid = bd.recarga_crear(user.id, estado[1], soles, cred, foto.file_unique_id, foto.file_id)
    if rid is None:
        await update.message.reply_text("⚠️ Esa captura ya fue enviada antes. Envía el comprobante de este pago.")
        return
    esperando_entrada.pop(user.id, None)
    bd.log_evento(user.id, "recarga_enviada")
    await update.message.reply_text(f"✅ Recibí tu comprobante (#{rid}). Te aviso apenas lo apruebe el administrador.",
                                    reply_markup=kb(VOLVER))
    try:
        await context.bot.send_photo(
            ADMIN_ID, foto.file_id,
            caption=(f"💵 Recarga #{rid}\nUsuario: {user.first_name or ''} (id {user.id})\n"
                     f"Paquete: S/ {soles:g} → {cred:g} créditos"),
            reply_markup=kb([("✅ Aprobar", f"rcok|{rid}"), ("❌ Rechazar", f"rcno|{rid}")]))
    except Exception as e:
        logging.error(f"No pude avisar al admin de la recarga #{rid}: {e}")


async def resolver_recarga(query, context):
    if query.from_user.id != ADMIN_ID:
        await _ack(query, "⚠️ Sin permisos.", True)
        return
    accion, rid = query.data.split("|")
    aprobar = accion == "rcok"
    r = bd.recarga_resolver(int(rid), "aprobada" if aprobar else "rechazada")
    if not r:
        await _ack(query, "Esa recarga ya estaba resuelta.", True)
        return
    await _ack(query, "✅ Aprobada" if aprobar else "❌ Rechazada")
    try:
        await query.message.edit_caption(caption=f"{query.message.caption}\n\n"
                                                 f"{'✅ APROBADA' if aprobar else '❌ RECHAZADA'}")
    except Exception:
        pass
    try:
        if aprobar:
            await context.bot.send_message(int(r["uid"]), f"✅ Recarga aprobada: +{r['creditos']:g} 🪙. "
                                                          f"Saldo: `{bd.creditos(r['uid'])}` 🪙", parse_mode="Markdown")
        else:
            await context.bot.send_message(int(r["uid"]), f"❌ Tu recarga #{rid} fue rechazada. "
                                                          "Si crees que es un error, escribe al administrador.")
    except Exception:
        pass


async def admin_recargas(query, context):
    if query.from_user.id != ADMIN_ID:
        await _ack(query, "⚠️ Sin permisos.", True)
        return
    pend = bd.recargas_pendientes()
    await _ack(query, f"{len(pend)} pendientes")
    for r in pend:
        try:
            await context.bot.send_photo(
                ADMIN_ID, r["file_id"], caption=f"💵 Recarga #{r['id']} (id {r['uid']})\nS/ {r['monto_pen']:g} → {r['creditos']:g} créditos",
                reply_markup=kb([("✅ Aprobar", f"rcok|{r['id']}"), ("❌ Rechazar", f"rcno|{r['id']}")]))
        except Exception as e:
            logging.error(f"Reenvío recarga: {e}")


# ==================== NÚMEROS VIRTUALES ====================
async def menu_servicios(query):
    await _ack(query)
    await _edit(query.message, "📱 *NÚMEROS VIRTUALES*\nSelecciona:",
                kb([(f"✈️ Telegram [{COSTOS_NUMERO['telegram']:g} 🪙]", "buy_telegram"),
                    (f"💬 WhatsApp [{COSTOS_NUMERO['whatsapp']:g} 🪙]", "buy_whatsapp")],
                   [(f"🌐 Google [{COSTOS_NUMERO['google']:g} 🪙]", "buy_google"),
                    (f"🤖 OpenAI [{COSTOS_NUMERO['openai']:g} 🪙]", "buy_openai")],
                   [("🔄 Revisar SMS activos", "btn_cheksms")], VOLVER))


async def comprar_servicio_especifico(query, context):
    user = query.from_user
    srv = query.data.replace("buy_", "")
    costo = COSTOS_NUMERO.get(srv)
    if costo is None:
        await _ack(query, "Servicio no válido.", True)
        return
    if limitado(user.id, "compra", 3, 120):
        await _ack(query, "⏳ Demasiadas compras seguidas, espera un momento.", True)
        return
    await _ack(query)
    _asegurar(user)
    volver = kb([("📱 Servicios", "btn_menu_servicios")], VOLVER)
    if not bd.cobrar(user.id, costo, f"numero_{srv}"):       # se cobra primero (atómico) y se devuelve si falla
        await _edit(query.message, "❌ Saldo insuficiente.", kb([("💵 Recargar", "btn_recarga")], VOLVER))
        return
    tzid, phone, estado = await asyncio.to_thread(comprar_numero_onlinesim, srv)
    if tzid:
        bd.numero_guardar(user.id, tzid, phone, srv, costo)   # persistente: sobrevive a reinicios
        await _premiar(context, user.id)
        await _edit(query.message, f"✅ ¡Adquirido para {srv.upper()}!\n📞 +{phone}\n\n"
                                   "Usa *Revisar SMS activos* cuando pidas el código (el número dura unos minutos).",
                    kb([("🔄 Revisar SMS activos", "btn_cheksms")], VOLVER))
    else:
        bd.abonar(user.id, costo, "reembolso_numero")
        await _edit(query.message, f"❌ Error: {estado}\nTu crédito fue devuelto.", volver)


async def chek_sms(query):
    await _ack(query)
    user = query.from_user
    volver = kb([("📱 Servicios", "btn_menu_servicios")], VOLVER)
    nums = bd.numeros_activos(user.id, minutos=60)
    if not nums:
        await _edit(query.message, "⚠️ No tienes números activos (duran unos minutos desde la compra).", volver)
        return
    txt = ""
    for n in nums:
        estado, sms, resp = await asyncio.to_thread(revisar_sms_onlinesim, n["tzid"])
        extra = ""
        if sms and sms[0].get("code"):
            bd.numero_marcar(n["id"], "recibido", str(sms[0]["code"]))
            extra = f"\n🔑 Código: {sms[0]['code']}"
        elif resp == "TZ_OVER_EMPTY":      # el proveedor cerró el número sin SMS
            if bd.numero_marcar(n["id"], "reembolsado"):
                bd.abonar(user.id, n["costo"], "reembolso_numero_vencido")
                estado = f"⌛ Venció sin SMS. Te devolvimos {n['costo']:g} 🪙."
        txt += f"📞 +{n['phone']} ({n['servicio']})\n{estado}{extra}\n\n"
    await _edit(query.message, txt.strip(), kb([("🔄 Volver a revisar", "btn_cheksms")], [("📱 Servicios", "btn_menu_servicios")], VOLVER))


# ==================== ADMIN ====================
async def panel_admin(query):
    if query.from_user.id != ADMIN_ID:
        await _ack(query, "⚠️ Sin permisos.", True)
        return
    await _ack(query)
    pend = len(bd.recargas_pendientes(50))
    await _edit(query.message,
                "🛠️ *PANEL ADMIN*\n`/dar <id> <cant>`\n`/broadcast <msg>`\n`/diag` (estado de fuentes y bases)\n`/metricas`\n`/lineas` (líneas de Apuesta Total)",
                kb([("📊 Métricas", "adm_metricas")], [(f"💵 Recargas pendientes ({pend})", "adm_recargas")], VOLVER))


async def admin_metricas(query):
    if query.from_user.id != ADMIN_ID:
        await _ack(query, "⚠️ Sin permisos.", True)
        return
    await _ack(query)
    await _edit(query.message, await asyncio.to_thread(bd.metricas_texto), kb([("⬅️ Panel", "btn_admin_panel")], VOLVER))


async def comando_metricas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(await asyncio.to_thread(bd.metricas_texto), parse_mode="Markdown")


async def comando_dar_creditos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or len(context.args) < 2:
        return
    try:
        cantidad = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Cantidad inválida.")
        return
    if not context.args[0].isdigit():
        await update.message.reply_text("❌ ID inválido.")
        return
    bd.abonar(context.args[0], cantidad, "admin")
    await update.message.reply_text(f"✅ Recargado a `{context.args[0]}`.", parse_mode="Markdown")


async def comando_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = " ".join(context.args)
    if not msg:
        return
    enviados = 0
    for uid in bd.todos_los_uids():
        try:
            await context.bot.send_message(chat_id=int(uid), text=f"📢 AVISO\n\n{msg}")
            enviados += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"✅ Broadcast enviado a {enviados} usuarios.")


def _diagnostico():
    lineas = []
    try:
        sp = motor.cargar_partidos(max_edad=0)
        est = {}
        for p in sp:
            est[p["estado"]] = est.get(p["estado"], 0) + 1
        lineas.append(f"StatPal: {len(sp)} partidos {est}")
    except Exception as e:
        lineas.append(f"StatPal: ERROR {type(e).__name__}")
    hoy = datetime.now(motor.LIMA).date()
    ok, fallan = [], []
    for liga in motor.LIGAS_ESPN:
        data = motor._get_seguro(f"{motor.ESPN}/{liga}/scoreboard", {"dates": hoy.strftime("%Y%m%d")}, ttl=0)
        (ok if data is not None else fallan).append(
            f"{liga}={len((data or {}).get('events', []))}" if data is not None else liga)
    lineas.append("ESPN hoy (liga=partidos): " + ", ".join(ok))
    if fallan:
        lineas.append("ESPN sin respuesta (código inválido?): " + ", ".join(fallan))
    for liga in ("per.1", "eng.1", "esp.1"):
        t = motor._tabla(liga)
        lineas.append(f"Tabla {liga} (ajuste por rival): " + (f"OK, {len(t['equipos'])} equipos" if t else "SIN DATOS → no se ajusta"))
    cal = motor._cal_actual()
    lineas.append("Calibración: " + (f"activa con {cal['n']} picks, ajustes {cal['offs']}" if cal else f"inactiva (requiere {motor.CAL_MIN} picks simples)"))
    picks = bd.picks_todos()
    lineas.append(f"Picks en BD: {len(picks)} ({sum(p['estado'] == 'pendiente' for p in picks)} pendientes)")
    lineas.append(f"Usuarios: {len(bd.todos_los_uids())} • BD: {bd.DB_PATH}")
    return "\n".join(lineas)


async def comando_diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    msg = await update.message.reply_text("🔎 Revisando fuentes...")
    await msg.edit_text(await asyncio.to_thread(_diagnostico))


# ==================== TAREAS PROGRAMADAS ====================
async def _enviar(bot, uid, texto, teclado=None):
    for pm, t in (("Markdown", texto), (None, _plano(texto))):
        try:
            await bot.send_message(int(uid), t, reply_markup=teclado, parse_mode=pm)
            return True
        except BadRequest:
            continue
        except Exception:
            return False
    return False


async def notificar_resultados(bot, cambios):
    por_usuario = defaultdict(list)
    for r in cambios:
        if r["estado"] in ("ganado", "perdido"):
            for uid in bd.vistas_de(r["id"]):
                por_usuario[uid].append(r)
    for uid, rs in por_usuario.items():
        lineas = ["🔔 *Resultados de tus picks*"]
        for r in rs[:12]:
            txt = " + ".join(l["texto"] for l in r["legs"])
            lineas.append(f"{'✅' if r['estado'] == 'ganado' else '❌'} {_corto(r['local'], 12)} vs "
                          f"{_corto(r['visita'], 12)} ({r['detalle']})\n    _{_limpio(txt)[:60]}_")
        await _enviar(bot, uid, "\n".join(lineas), kb([("📈 Ver récord", "btn_record")]))
        await asyncio.sleep(0.05)


async def notificar_consultas(bot, consultas):
    """Avisa a cada usuario cómo le fue en el partido que consultó."""
    pt = {"segura": "🟢 Segura", "equilibrada": "🟡 Equilibrada", "arriesgada": "🔴 Arriesgada", "pick": "☝️ Pick"}
    for c in consultas:
        u = bd.get_usuario(c["uid"])
        if not u or not u["avisos"]:
            continue
        r = c["resultado"]
        s = (f"🏁 *Terminó tu partido*\n⚽ *{_corto(c['local'], 14)}* `{r['gl']}-{r['gv']}` *{_corto(c['visita'], 14)}*")
        if r.get("am") is not None:
            s += f"  🟨 `{int(r['am'])}`"
        if r.get("cor") is not None:
            s += f"  ⛳ `{int(r['cor'])}`"
        s += "\n"
        if not r["tiers"]:
            s += "\n_No habías armado selecciones para este partido._"
        for t in r["tiers"]:
            est = {"ganada": "✅ *GANASTE*", "perdida": "❌ perdió", "nula": "➖ sin datos para calificar"}[t["estado"]]
            s += f"\n{pt.get(t['tipo'], t['tipo'])}: {est}\n"
            for l in t["legs"]:
                s += f"   {'✅' if l['ok'] else '❌' if l['ok'] is False else '❔'} {_limpio(l['texto'])}\n"
        await _enviar(bot, c["uid"], s, kb([("📜 Mi historial", "btn_hist")]))
        await asyncio.sleep(0.05)


async def job_liquidar(context: ContextTypes.DEFAULT_TYPE):
    try:
        cambios = await asyncio.to_thread(motor.liquidar_pendientes)
        if cambios:
            await asyncio.to_thread(motor.recalcular_calibracion)
            await notificar_resultados(context.bot, cambios)
        consultas = await asyncio.to_thread(motor.liquidar_consultas)
        if consultas:
            await notificar_consultas(context.bot, consultas)
    except Exception as e:
        logging.error(f"job_liquidar: {e}")


async def job_picks(context: ContextTypes.DEFAULT_TYPE):
    """Deja los picks calculados para que quien los pida reciba la respuesta al instante."""
    for pe in (False, True):
        try:
            await asyncio.to_thread(motor.fijas_del_dia, pe, True)
        except Exception as e:
            logging.error(f"job_picks: {e}")


async def job_matutino(context: ContextTypes.DEFAULT_TYPE):
    res_todo = res_pe = None
    try:
        res_pe = await asyncio.to_thread(motor.fijas_del_dia, True, True)
        res_todo = await asyncio.to_thread(motor.fijas_del_dia, False, True)
    except Exception as e:
        logging.error(f"job_matutino: {e}")
    if not (res_todo or res_pe):
        return
    cuerpo = "\n".join(motor.resumen_corto(r, pe) for r, pe in ((res_pe, True), (res_todo, False)) if r)
    texto = ("☀️ *Buenos días*\n\n" + cuerpo +
             "\n_Toca *Picks* para verlos todos con cuota justa y confianza._\n🔞 _Solo +18._")
    for uid in bd.todos_los_uids(solo_avisos=True):
        await _enviar(context.bot, uid, texto, kb([("🏆 Ver picks", "menu_picks")]))
        await asyncio.sleep(0.05)
    if CANAL_ID:
        for r in (res_pe, res_todo):
            if r:
                await _enviar(context.bot, CANAL_ID, r["texto"])


async def job_noche(context: ContextTypes.DEFAULT_TYPE):
    """Publica el récord completo (con los fallos incluidos) en el canal."""
    if not CANAL_ID:
        return
    try:
        await asyncio.to_thread(motor.liquidar_pendientes)
        await _enviar(context.bot, CANAL_ID, await asyncio.to_thread(motor.record_texto))
    except Exception as e:
        logging.error(f"job_noche: {e}")


# ==================== ENRUTADOR DE BOTONES ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ""
    user = query.from_user
    _asegurar(user)
    if limitado(user.id, "cb", 15, 30):
        await _ack(query, "⏳ Vas muy rápido, espera unos segundos.", True)
        return
    accion = data.split("|")[0]
    bd.log_evento(user.id, data if data.startswith("buy_") else accion)

    if data == "btn_start":
        await _ack(query)
        await mostrar_menu_principal(query)
    elif data == "menu_picks":
        await menu_picks(query)
    elif data == "menu_partido":
        await menu_partido(query)
    elif data == "menu_cuenta":
        await menu_cuenta(query)
    elif data == "btn_cuenta":
        await ver_cuenta(query)
    elif data == "btn_bonus":
        await reclamar_bonus(query)
    elif data == "btn_avisos":
        await alternar_avisos(query)
    elif data == "btn_ref":
        await invitar(query, context)
    elif data == "btn_hist":
        await historial_callback(query)
    elif accion == "gan":
        await ganar_callback(query)
    elif data == "btn_recarga":
        await recarga_menu(query)
    elif accion == "rc":
        await recarga_paquete(query)
    elif accion in ("rcok", "rcno"):
        await resolver_recarga(query, context)
    elif data == "btn_admin_panel":
        await panel_admin(query)
    elif data == "adm_metricas":
        await admin_metricas(query)
    elif data == "adm_recargas":
        await admin_recargas(query, context)
    elif data == "manual_match":
        await solicitar_analisis_manual(query)
    elif accion == "hoy":
        await mostrar_partidos(query, int(data.split("|")[1]))
    elif accion == "m":
        await partido_seleccionado(query, context)
    elif accion == "ref":
        await refrescar_partido(query, context)
    elif data == "btn_record":
        await record_handler(query)
    elif data in ("btn_fijas", "btn_fijas_pe"):
        await fijas_handler(query, context)
    elif data == "btn_menu_servicios":
        await menu_servicios(query)
    elif data.startswith("buy_"):
        await comprar_servicio_especifico(query, context)
    elif data == "btn_cheksms":
        await chek_sms(query)
    else:
        await _ack(query)


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Error no controlado", exc_info=context.error)


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("Falta TELEGRAM_TOKEN. Copia .env.example a .env y complétalo.")
    if not ADMIN_ID:
        print("⚠️ ADMIN_ID no configurado: los comandos de administrador no funcionarán.")
    bd.migrar_json()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('dar', comando_dar_creditos))
    app.add_handler(CommandHandler('broadcast', comando_broadcast))
    app.add_handler(CommandHandler('diag', comando_diag))
    app.add_handler(CommandHandler('metricas', comando_metricas))
    app.add_handler(CommandHandler('valor', comando_valor))
    app.add_handler(CommandHandler('record', comando_record))
    app.add_handler(CommandHandler('ganar', comando_ganar))
    app.add_handler(CommandHandler('historial', comando_historial))
    app.add_handler(CommandHandler('lineas', comando_lineas))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.PHOTO, manejar_foto))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_texto_usuario))
    app.add_error_handler(error_handler)
    if app.job_queue:
        app.job_queue.run_repeating(job_liquidar, interval=1800, first=120)
        app.job_queue.run_repeating(job_picks, interval=1800, first=20)
        app.job_queue.run_daily(job_matutino, time=dtime(8, 0, tzinfo=motor.LIMA))
        app.job_queue.run_daily(job_noche, time=dtime(22, 30, tzinfo=motor.LIMA))
    else:
        print('⚠️ Sin JobQueue: instala  pip install "python-telegram-bot[job-queue]"')
    print("¡Bot The Android activo!")
    app.run_polling()


if __name__ == '__main__':
    main()
