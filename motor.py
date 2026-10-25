"""Motor de análisis de The Android: fuentes de datos, proyección, picks, récord y calibración.
No depende de Telegram; la interfaz está en bot.py."""
import difflib
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

import db as bd

STATPAL_KEY = os.getenv("STATPAL_KEY", "")
STATPAL_URL = "https://statpal.io/api/v2/soccer/matches/live"

LIMA = timezone(timedelta(hours=-5))          # Perú no usa horario de verano
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]

ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
ESPN_V2 = "https://site.api.espn.com/apis/v2/sports/soccer"      # standings viven aquí
LIGAS_ESPN = [
    "per.1", "arg.1", "bra.1", "col.1", "chi.1", "ecu.1", "uru.1", "par.1",
    "mex.1", "usa.1", "eng.1", "esp.1", "ita.1", "ger.1", "fra.1", "por.1",
    "ned.1", "tur.1", "conmebol.libertadores", "conmebol.sudamericana",
    "uefa.champions", "uefa.europa",
]
LINEAS_GOLES = (1.5, 2.5, 3.5, 4.5)   # Apuesta Total no ofrece 0.5
GOLES_PRIOR = 1.35
K_PRIOR = 4
N_PARTIDOS = 8
N_STATS = 5

SEP = "━━━━━━━━━━━━━━━━━━━━"
CONF_ICONO = {"alta": "🟢", "media": "🟡", "baja": "🔴"}
_RANK = {"baja": 0, "media": 1, "alta": 2}

# Líneas que ofrece la casa (Apuesta Total). Solo filtran QUÉ líneas se muestran antes del partido:
# las probabilidades salen siempre del modelo con datos reales. Se editan con /lineas (admin).
LINEAS_DEFAULT = {"goles": [1.5, 2.5, 3.5, 4.5],
                  "amarillas": [2.5, 3.5, 4.5, 5.5, 6.5],
                  "corners": [7.5, 8.5, 9.5, 10.5, 11.5, 12.5, 13.5]}
_lin_cache = {"ts": 0, "val": None}


def lineas_actuales():
    if _lin_cache["val"] is None or time.time() - _lin_cache["ts"] > 60:
        out = {k: list(v) for k, v in LINEAS_DEFAULT.items()}
        try:
            raw = bd.kv_get("lineas", "")
            for k, v in (json.loads(raw) if raw else {}).items():
                if k in out and v:
                    out[k] = sorted(float(x) for x in v)
        except Exception:
            pass
        _lin_cache.update(ts=time.time(), val=out)
    return _lin_cache["val"]


def guardar_lineas(mercado, valores):
    """valores=None restablece todo. Refresca los picks en caché."""
    if valores is None:
        bd.kv_set("lineas", "")
    else:
        try:
            actual = json.loads(bd.kv_get("lineas", "") or "{}")
        except Exception:
            actual = {}
        actual[mercado] = sorted(set(valores))
        bd.kv_set("lineas", json.dumps(actual))
    _lin_cache.update(ts=0, val=None)
    _cache_fijas.clear()


# ==================== UTILIDADES ====================
ALIAS = {"dep": "deportivo", "atl": "atletico", "ath": "athletic", "utd": "united",
         "sp": "sporting", "int": "inter",
         "de": "", "del": "", "la": "", "el": "", "a": "", "fc": "", "cf": "", "cd": "",
         "sc": "", "ac": "", "club": ""}


def normalizar_texto(texto):
    texto = re.sub(r'\b(today|live)\b', ' ', str(texto), flags=re.IGNORECASE)
    nfkd = unicodedata.normalize('NFKD', texto)
    sin_tildes = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    sin_tildes = re.sub(r'[^a-z0-9 ]', ' ', sin_tildes)
    tokens = [ALIAS.get(t, t) for t in sin_tildes.split()]
    return " ".join(t for t in tokens if t)


def _lista(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _limpio(t):
    return re.sub(r'[_*`\[\]]', '', str(t))


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _media(*vals):
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def _prom(lst):
    return sum(lst) / len(lst) if lst else None


def _fmt(x, dec=1):
    return "s/d" if x is None else f"{x:.{dec}f}"


def _fmt_inicio(dt):
    return f"{DIAS[dt.weekday()]} {dt:%d/%m} • {dt:%H:%M} (hora Lima)"


_http_cache = {}


def _get(url, params=None, ttl=300):
    key = (url, tuple(sorted((params or {}).items())))
    hit = _http_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    r = requests.get(url, params=params, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()
    if len(_http_cache) > 800:
        for k in sorted(_http_cache, key=lambda k: _http_cache[k][0])[:300]:
            _http_cache.pop(k, None)
    _http_cache[key] = (time.time(), data)
    return data


def _get_seguro(url, params=None, ttl=300):
    try:
        return _get(url, params, ttl)
    except Exception as e:
        logging.debug(f"GET fallido {url}: {e}")
        return None


# ==================== COINCIDENCIA DE NOMBRES ====================
def _sim(q, nombre):
    n = normalizar_texto(nombre)
    if not q or not n:
        return 0.0
    if len(n) >= 4 and len(q) >= 4 and (n in q or q in n):
        return 1.0
    return difflib.SequenceMatcher(None, q, n).ratio()


def _partes_busqueda(texto):
    return [normalizar_texto(p)
            for p in re.split(r"\s+(?:vs|v|contra|-)\s+", texto, flags=re.I) if p.strip()]


def _puntaje(partes, p):
    if len(partes) >= 2:
        a1, a2 = _sim(partes[0], p["local"]), _sim(partes[1], p["visita"])
        b1, b2 = _sim(partes[0], p["visita"]), _sim(partes[1], p["local"])
        s1 = (a1 + a2) / 2 if min(a1, a2) >= 0.6 else 0.0
        s2 = (b1 + b2) / 2 if min(b1, b2) >= 0.6 else 0.0
        return max(s1, s2)
    s = max(_sim(partes[0], p["local"]), _sim(partes[0], p["visita"]))
    return s if s >= 0.8 else 0.0


def buscar_en(partes, lista):
    mejor, mejor_score = None, 0.0
    for p in lista:
        s = _puntaje(partes, p)
        if s and p["estado"] == "live":
            s += 0.05
        if s > mejor_score:
            mejor, mejor_score = p, s
    return mejor if mejor_score >= 0.72 else None


def _mismo_partido(a, b):
    def par(x, y):
        return _sim(normalizar_texto(x["local"]), y["local"]) >= 0.75 and \
               _sim(normalizar_texto(x["visita"]), y["visita"]) >= 0.75
    return par(a, b)


# ==================== STATPAL (EN VIVO) ====================
_cache_sp = {"ts": 0, "partidos": []}


def _estado(status):
    s = str(status).strip()
    if s.isdigit():
        return "live", f"{s}'"
    if s.upper() == "HT":
        return "live", "Descanso"
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        return "scheduled", s
    if s.upper() in ("FT", "AET", "PEN", "AP"):
        return "finished", "Final"
    return "other", s


def _goles(g):
    return int(g) if str(g).isdigit() else None


def cargar_partidos(max_edad=30):
    if time.time() - _cache_sp["ts"] < max_edad and _cache_sp["partidos"]:
        return _cache_sp["partidos"]
    r = requests.get(STATPAL_URL, params={"access_key": STATPAL_KEY}, timeout=15)
    r.raise_for_status()
    data = r.json()
    partidos = []
    for liga in _lista(data["live_matches"]["league"]):
        for m in _lista(liga.get("match")):
            estado, minuto = _estado(m.get("status", ""))
            evs = _lista((m.get("events") or {}).get("event"))
            partidos.append({
                "liga": liga.get("name", ""),
                "local": m["home"]["name"],
                "visita": m["away"]["name"],
                "gl": _goles(m["home"].get("goals")),
                "gv": _goles(m["away"].get("goals")),
                "estado": estado,
                "minuto": minuto,
                "inicio": None,
                "hora_feed": f"{m.get('time', '')} ({m.get('date', '')})",
                "amarillas": sum(e.get("type") == "yellowcard" for e in evs),
                "rojas": sum(e.get("type") in ("redcard", "yellowred") for e in evs),
                "fuente": "StatPal",
            })
    _cache_sp.update(ts=time.time(), partidos=partidos)
    return partidos


# ==================== ESPN (FIXTURES + HISTORIAL) ====================
def _score(c):
    s = c.get("score")
    if isinstance(s, dict):
        s = s.get("displayValue", s.get("value"))
    v = _num(s)
    return int(v) if v is not None else None


def _parse_evento(ev, liga_code, liga_nombre):
    comp = (ev.get("competitions") or [{}])[0]
    cs = comp.get("competitors") or []
    home = next((c for c in cs if c.get("homeAway") == "home"), None)
    away = next((c for c in cs if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    tipo = (ev.get("status") or {}).get("type") or {}
    estado = {"pre": "scheduled", "in": "live", "post": "finished"}.get(tipo.get("state"), "other")
    inicio = None
    try:
        inicio = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(LIMA)
    except Exception:
        pass
    minuto = {"finished": "Final", "scheduled": ""}.get(
        estado, (ev.get("status") or {}).get("displayClock") or tipo.get("shortDetail", ""))
    return {
        "liga": liga_nombre, "liga_code": liga_code, "event_id": ev.get("id"),
        "local": home["team"]["displayName"], "visita": away["team"]["displayName"],
        "id_local": home["team"].get("id"), "id_visita": away["team"].get("id"),
        "gl": _score(home), "gv": _score(away),
        "estado": estado, "minuto": minuto, "inicio": inicio, "hora_feed": "",
        "amarillas": None, "rojas": None, "fuente": "ESPN",
    }


def _scoreboard(liga_code, fecha):
    data = _get_seguro(f"{ESPN}/{liga_code}/scoreboard", {"dates": fecha.strftime("%Y%m%d")}, ttl=60)
    if not data:
        return []
    nombre = ((data.get("leagues") or [{}])[0]).get("name", liga_code)
    return [p for p in (_parse_evento(ev, liga_code, nombre) for ev in data.get("events", [])) if p]


def _scoreboards(fechas):
    tareas = [(l, f) for l in LIGAS_ESPN for f in fechas]
    with ThreadPoolExecutor(max_workers=12) as ex:
        resultados = list(ex.map(lambda t: _scoreboard(*t), tareas))
    vistos, salida = set(), []
    for lista in resultados:
        for p in lista:
            if p["event_id"] not in vistos:
                vistos.add(p["event_id"])
                salida.append(p)
    return salida


def buscar_espn(partes):
    hoy = datetime.now(LIMA).date()
    for offsets in ([0, 1], [-1, 2, 3]):
        lista = _scoreboards([hoy + timedelta(days=o) for o in offsets])
        m = buscar_en(partes, lista)
        if m:
            return m
    return None


def evento_por_id(event_id, liga):
    """Recupera un partido concreto (estado fresco) sin recorrer todas las ligas."""
    hoy = datetime.now(LIMA).date()
    for d in (hoy, hoy + timedelta(days=1), hoy - timedelta(days=1), hoy + timedelta(days=2)):
        for p in _scoreboard(liga, d):
            if str(p["event_id"]) == str(event_id):
                return p
    return None


_cache_lista = {"ts": 0, "lista": []}


def listar_partidos(max_edad=90):
    """Partidos en vivo y próximos (30 h). Liga 1 primero, luego en vivo, luego por hora."""
    if time.time() - _cache_lista["ts"] < max_edad and _cache_lista["lista"]:
        return _cache_lista["lista"]
    hoy, ahora = datetime.now(LIMA).date(), datetime.now(LIMA)
    cand = _scoreboards([hoy, hoy + timedelta(days=1)])
    lista = [p for p in cand
             if (p["estado"] == "live"
                 or (p["estado"] == "scheduled" and p["inicio"] and timedelta(0) < p["inicio"] - ahora < timedelta(hours=30)))
             and p["event_id"]]
    lista.sort(key=lambda p: (p["liga_code"] != "per.1", p["estado"] != "live",
                              p["inicio"] or ahora))
    _cache_lista.update(ts=time.time(), lista=lista)
    return lista


# ---- tabla de posiciones y ajuste por fuerza del rival ----
def _tabla(liga):
    """{'equipos': {id: {'gf': goles a favor por partido, 'ga': en contra}}, 'media': promedio de liga} o None."""
    data = _get_seguro(f"{ESPN_V2}/{liga}/standings", ttl=6 * 3600)
    if not data:
        return None
    entries = []

    def walk(node):
        if isinstance(node, dict):
            st = node.get("standings")
            if isinstance(st, dict):
                entries.extend(st.get("entries", []))
            for ch in node.get("children") or []:
                walk(ch)
    walk(data)
    eq = {}
    for e in entries:
        stats = {s.get("name"): _num(s.get("value")) for s in e.get("stats", [])}
        gp, gf, ga = stats.get("gamesPlayed"), stats.get("pointsFor"), stats.get("pointsAgainst")
        tid = str((e.get("team") or {}).get("id"))
        if gp and gp >= 4 and gf is not None and ga is not None:
            eq[tid] = {"gf": gf / gp, "ga": ga / gp}
    if len(eq) < 6:
        return None
    return {"equipos": eq, "media": sum(v["gf"] for v in eq.values()) / len(eq)}


def _suave(f):
    """Limita y reduce a la mitad el ajuste para no sobrecorregir."""
    return 1 + 0.5 * (max(0.6, min(1.6, f)) - 1)


def _ajustar(gf, gc, rival_id, tabla):
    if not tabla or rival_id is None:
        return gf, gc
    r = tabla["equipos"].get(str(rival_id))
    if not r:
        return gf, gc
    m = tabla["media"]
    f_gf = _suave(m / r["ga"]) if r["ga"] > 0 else 1.0     # rival que concede poco: sus goles valen más
    f_gc = _suave(m / r["gf"]) if r["gf"] > 0 else 1.0     # rival que anota mucho: recibir goles pesa menos
    return gf * f_gf, gc * f_gc


def _historial(liga, team_id):
    data = _get_seguro(f"{ESPN}/{liga}/teams/{team_id}/schedule", ttl=1800)
    if not data:
        return []
    out = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        tipo = ((comp.get("status") or ev.get("status") or {}).get("type")) or {}
        if not (tipo.get("completed") or tipo.get("state") == "post"):
            continue
        cs = comp.get("competitors") or []
        yo = next((c for c in cs if str((c.get("team") or {}).get("id")) == str(team_id)), None)
        rival = next((c for c in cs if c is not yo), None)
        if not yo or not rival:
            continue
        gf, gc = _score(yo), _score(rival)
        if gf is None or gc is None:
            continue
        out.append({"fecha": ev.get("date", ""), "id": ev.get("id"),
                    "local": yo.get("homeAway") == "home", "gf": gf, "gc": gc,
                    "rival": (rival.get("team") or {}).get("id")})
    out.sort(key=lambda h: h["fecha"])
    return out[-N_PARTIDOS:]


def _stats_evento(liga, event_id, ttl=86400):
    data = _get_seguro(f"{ESPN}/{liga}/summary", {"event": event_id}, ttl=ttl)
    if not data:
        return {}
    out = {}
    for t in (data.get("boxscore") or {}).get("teams", []):
        tid = str((t.get("team") or {}).get("id"))
        d = {}
        for s in t.get("statistics", []):
            nombre = str(s.get("name", "")).lower()
            val = _num(s.get("displayValue"))
            if val is None:
                continue
            if "yellow" in nombre:
                d["amarillas"] = val
            elif "corner" in nombre:
                d["corners"] = val
        out[tid] = d
    return out


def promedios(liga, team_id, es_local):
    hist = _historial(liga, team_id)
    if not hist:
        return None
    tabla = _tabla(liga)
    for h in hist:
        h["agf"], h["agc"] = _ajustar(h["gf"], h["gc"], h.get("rival"), tabla)
    rol = [h for h in hist if h["local"] == es_local]
    r = {
        "n": len(hist),
        "gf": _prom([h["gf"] for h in hist]), "gc": _prom([h["gc"] for h in hist]),
        "agf": _prom([h["agf"] for h in hist]), "agc": _prom([h["agc"] for h in hist]),
        "n_rol": len(rol),
        "gf_rol": _prom([h["gf"] for h in rol]), "gc_rol": _prom([h["gc"] for h in rol]),
        "agf_rol": _prom([h["agf"] for h in rol]), "agc_rol": _prom([h["agc"] for h in rol]),
        "ajustado": tabla is not None,
        "forma": "".join("🟢" if h["gf"] > h["gc"] else "🟡" if h["gf"] == h["gc"] else "🔴"
                         for h in hist[-5:]),
    }
    ids = [h["id"] for h in hist[-N_STATS:] if h["id"]]
    with ThreadPoolExecutor(max_workers=5) as ex:
        res = list(ex.map(lambda i: _stats_evento(liga, i), ids))
    am, cor, corc = [], [], []
    tid = str(team_id)
    for st in res:
        mio = st.get(tid) or {}
        otro = next((v for k, v in st.items() if k != tid), {})
        if "amarillas" in mio:
            am.append(mio["amarillas"])
        if "corners" in mio:
            cor.append(mio["corners"])
        if "corners" in otro:
            corc.append(otro["corners"])
    r.update(am=_prom(am), cor=_prom(cor), cor_c=_prom(corc), n_stats=len(am))
    return r


# ==================== PROYECCIÓN ====================
def _poisson(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _over_prob(lam, linea, base=0):
    need = math.floor(linea - base) + 1
    if need <= 0:
        return 1.0
    return max(0.0, 1 - sum(_poisson(k, lam) for k in range(need)))


def _val(r, k):
    """Usa el promedio ajustado por rival si existe; si no, el crudo."""
    v = r.get("a" + k)
    return v if v is not None else r.get(k)


def proyectar(a, b, gl0=0, gv0=0, frac=1.0, am0=None, cor0=None, en_vivo=False):
    if not a or not b:
        return None

    def aj(x, n):
        return None if x is None else (x * n + GOLES_PRIOR * K_PRIOR) / (n + K_PRIOR)

    ataque_l = aj(_val(a, "gf_rol"), a["n_rol"]) if a["n_rol"] >= 3 else aj(_val(a, "gf"), a["n"])
    defensa_v = aj(_val(b, "gc_rol"), b["n_rol"]) if b["n_rol"] >= 3 else aj(_val(b, "gc"), b["n"])
    ataque_v = aj(_val(b, "gf_rol"), b["n_rol"]) if b["n_rol"] >= 3 else aj(_val(b, "gf"), b["n"])
    defensa_l = aj(_val(a, "gc_rol"), a["n_rol"]) if a["n_rol"] >= 3 else aj(_val(a, "gc"), a["n"])
    if None in (ataque_l, defensa_v, ataque_v, defensa_l):
        return None
    lh = max((ataque_l + defensa_v) / 2, 0.15) * frac
    la = max((ataque_v + defensa_l) / 2, 0.15) * frac
    p1 = px = p2 = btts = tot = 0.0
    for i in range(9):
        for j in range(9):
            p = _poisson(i, lh) * _poisson(j, la)
            h, v = gl0 + i, gv0 + j
            tot += p
            if h > v:
                p1 += p
            elif h == v:
                px += p
            else:
                p2 += p
            if h > 0 and v > 0:
                btts += p
    am_l, am_v = a.get("am"), b.get("am")
    am_lam = ((am_l + am_v) * frac
              if am_l is not None and am_v is not None and (not en_vivo or am0 is not None) else None)
    cor_l = _media(a.get("cor"), b.get("cor_c"))
    cor_v = _media(b.get("cor"), a.get("cor_c"))
    cor_lam = ((cor_l + cor_v) * frac
               if cor_l is not None and cor_v is not None and (not en_vivo or cor0 is not None) else None)
    return {
        "fh": gl0 + lh, "fa": gv0 + la, "lt": lh + la, "gbase": gl0 + gv0,
        "p1": p1 / tot, "px": px / tot, "p2": p2 / tot, "btts": btts / tot,
        "am_lam": am_lam, "am_base": am0 or 0,
        "cor_lam": cor_lam, "cor_base": cor0 or 0,
        "n_min": min(a["n"], b["n"]),
        "n_st": min(a.get("n_stats", 0), b.get("n_stats", 0)),
        "ajustado": bool(a.get("ajustado") and b.get("ajustado")),
        "en_vivo": en_vivo,
    }


# ==================== CONFIANZA Y CALIBRACIÓN ====================
def _conf_nivel(pr, grupo):
    if grupo in ("tarj", "corn"):
        n = pr.get("n_st", 0)
        return "alta" if n >= 5 else "media" if n >= 3 else "baja"
    n = pr.get("n_min", 0)
    return "alta" if n >= 8 else "media" if n >= 6 else "baja"


CAL_MIN = 100                      # picks simples liquidados necesarios para activar la calibración
CAL_BINS = [(0.0, 0.55), (0.55, 0.70), (0.70, 0.80), (0.80, 1.01)]
_cal_cache = {"ts": 0, "val": None}


def _raw_leg(r):
    leg = r["legs"][0]
    return leg["spec"].get("raw", leg["prob"])


def recalcular_calibracion():
    """Compara lo que el modelo decía con lo que pasó (picks simples) y guarda un ajuste por tramo."""
    rs = [r for r in bd.picks_todos()
          if r["tipo"] == "pick" and r["estado"] in ("ganado", "perdido") and len(r["legs"]) == 1]
    if len(rs) < CAL_MIN:
        bd.kv_set("calibracion", "")
        _cal_cache.update(ts=0, val=None)
        return None
    offs = []
    for lo, hi in CAL_BINS:
        sel = [r for r in rs if lo <= _raw_leg(r) < hi]
        if len(sel) < 15:
            offs.append(0.0)
            continue
        pred = sum(_raw_leg(r) for r in sel) / len(sel)
        emp = sum(r["estado"] == "ganado" for r in sel) / len(sel)
        w = len(sel) / (len(sel) + 40)               # encoge hacia 0 cuando hay pocos datos
        offs.append(round(w * (emp - pred), 4))
    val = {"n": len(rs), "offs": offs}
    bd.kv_set("calibracion", json.dumps(val))
    _cal_cache.update(ts=time.time(), val=val)
    return val


def _cal_actual():
    if time.time() - _cal_cache["ts"] > 600:
        try:
            raw = bd.kv_get("calibracion", "")
            _cal_cache.update(ts=time.time(), val=json.loads(raw) if raw else None)
        except Exception:
            _cal_cache.update(ts=time.time(), val=None)
    return _cal_cache["val"]


def calibrar(p):
    cal = _cal_actual()
    if not cal:
        return p
    for (lo, hi), off in zip(CAL_BINS, cal["offs"]):
        if lo <= p < hi:
            return min(0.98, max(0.02, p + off))
    return p


# ==================== REPORTE ====================
def _corto(n, k=14):
    n = _limpio(n)
    return n if len(n) <= k else n[:k - 1] + "…"


def _barra(p, n=10):
    k = max(0, min(n, round(p * n)))
    return "█" * k + "░" * (n - k)


def _cuenta_regresiva(inicio):
    seg = (inicio - datetime.now(LIMA)).total_seconds()
    if seg <= 0:
        return None
    h, m = int(seg // 3600), int(seg % 3600 // 60)
    return f"{h // 24}d {h % 24}h" if h >= 24 else f"{h}h {m:02d}m"


def _minuto_num(m):
    s = str(m)
    if "descanso" in s.lower() or s.upper() == "HT":
        return 45
    mt = re.match(r"\s*(\d+)", s)
    return int(mt.group(1)) if mt else None


def _bloque_equipo(nombre, r, icono, rol_txt, rol_icono):
    if not r:
        return f"{icono} *{nombre}*\n   _sin historial reciente en el feed_"
    s = f"{icono} *{nombre}*  {r.get('forma', '')}\n"
    s += f"   ⚽ `{_fmt(r['gf'])}` a favor · `{_fmt(r['gc'])}` en contra\n"
    if r["n_rol"] >= 3:
        s += f"   {rol_icono} {rol_txt}: `{_fmt(r['gf_rol'])}` / `{_fmt(r['gc_rol'])}` ({r['n_rol']} part.)\n"
    extra = []
    if r.get("am") is not None:
        extra.append(f"🟨 `{_fmt(r['am'])}` amarillas")
    if r.get("cor") is not None:
        extra.append(f"⛳ `{_fmt(r['cor'])}` córners")
    if extra:
        s += "   " + " · ".join(extra) + "\n"
    return s.rstrip("\n")


def _bloque_proyeccion(pr, local, visita, en_vivo):
    L, V = _corto(local, 14), _corto(visita, 14)
    titulo = ("🎯 *PROYECCIÓN EN VIVO* _(ajustada al minuto y marcador)_" if en_vivo
              else "🎯 *PROYECCIÓN PRE-PARTIDO*")
    o15 = _over_prob(pr["lt"], 1.5, pr["gbase"])
    o25 = _over_prob(pr["lt"], 2.5, pr["gbase"])
    filas1 = [(L, pr["p1"]), ("Empate", pr["px"]), (V, pr["p2"])]
    filas2 = [("Más 1.5", o15), ("Más 2.5", o25), ("Ambos anotan", pr["btts"])]
    fmt = lambda f: "\n".join(f"{n:<14} {_barra(p)} {p * 100:>3.0f}%" for n, p in f)
    s = (f"{titulo}\n⚽ Goles finales esperados: `{pr['fh']:.1f} - {pr['fa']:.1f}`\n"
         f"```\n{fmt(filas1)}\n\n{fmt(filas2)}\n```")
    extra = []
    if pr["am_lam"] is not None:
        extra.append(f"🟨 Amarillas (final): `~{pr['am_base'] + pr['am_lam']:.1f}`")
    if pr["cor_lam"] is not None:
        extra.append(f"⛳ Tiros de esquina (final): `~{pr['cor_base'] + pr['cor_lam']:.1f}`")
    if extra:
        s += "\n" + "  |  ".join(extra)
    nivel = _conf_nivel(pr, "gol")
    s += f"\n🔒 Confianza (goles): {CONF_ICONO[nivel]} *{nivel}* _({pr['n_min']} partidos de historial)_"
    if pr.get("ajustado"):
        s += "\n⚖️ _Ajustado por la fuerza de los rivales enfrentados._"
    return s


def _candidatos(pr, local, visita):
    """(grupo, texto, probabilidad calibrada, spec). spec guarda prob cruda ('raw') y confianza ('conf')."""
    L, V = _corto(local, 20), _corto(visita, 20)
    p1, px, p2 = pr["p1"], pr["px"], pr["p2"]
    c = [("res", f"Gana {L}", p1, {"k": "res", "v": "1"}),
         ("res", f"Gana {V}", p2, {"k": "res", "v": "2"}),
         ("res", f"{L} o empate (1X)", p1 + px, {"k": "res", "v": "1X"}),
         ("res", f"{V} o empate (X2)", p2 + px, {"k": "res", "v": "X2"}),
         ("res", f"{L} o {V}, sin empate (12)", p1 + p2, {"k": "res", "v": "12"})]
    lineas = lineas_actuales()
    for linea in lineas["goles"]:
        po = _over_prob(pr["lt"], linea, pr["gbase"])
        c += [("gol", f"Más de {linea} goles", po, {"k": "goles", "lado": "mas", "linea": linea}),
              ("gol", f"Menos de {linea} goles", 1 - po, {"k": "goles", "lado": "menos", "linea": linea})]
    c += [("gol", "Ambos equipos anotan: Sí", pr["btts"], {"k": "btts", "si": True}),
          ("gol", "Ambos equipos anotan: No", 1 - pr["btts"], {"k": "btts", "si": False})]
    for clave, kind, lam_k, base_k, nombre, maxl in (("tarj", "amarillas", "am_lam", "am_base", "tarjetas amarillas", 12),
                                                      ("corn", "corners", "cor_lam", "cor_base", "tiros de esquina", 20)):
        if pr[lam_k] is None:
            continue
        esperado = pr[base_k] + pr[lam_k]
        ventana = 1.6 if clave == "tarj" else 2.6
        # antes del partido solo líneas que la casa ofrece; en vivo las líneas se mueven, así que basta la ventana
        permitidas = None if pr.get("en_vivo") else set(lineas["amarillas" if clave == "tarj" else "corners"])
        for k in range(maxl):
            linea = k + 0.5
            if linea < pr[base_k] or abs(linea - esperado) > ventana:
                continue
            if permitidas is not None and linea not in permitidas:
                continue
            po = _over_prob(pr[lam_k], linea, pr[base_k])
            c += [(clave, f"Más de {linea} {nombre}", po, {"k": kind, "lado": "mas", "linea": linea}),
                  (clave, f"Menos de {linea} {nombre}", 1 - po, {"k": kind, "lado": "menos", "linea": linea})]
    out = []
    for g, t, p, spec in c:
        spec = dict(spec)
        spec["raw"] = round(p, 4)
        spec["conf"] = _conf_nivel(pr, g)
        out.append((g, t, calibrar(p), spec))
    return out


def _armar_combinadas(cands):
    grupos = {}
    for g, t, p, spec in cands:
        grupos.setdefault(g, []).append((p, t, spec))

    def elegir(lo, hi, max_legs):
        legs = []
        for lst in grupos.values():
            ok = [x for x in lst if lo <= x[0] < hi]
            if ok:
                legs.append(max(ok, key=lambda x: x[0]))
        legs.sort(key=lambda x: x[0], reverse=True)
        return legs[:max_legs]

    return [("segura", "🟢 *SEGURA*", elegir(0.70, 0.90, 2)),
            ("equilibrada", "🟡 *EQUILIBRADA*", elegir(0.55, 0.70, 3)),
            ("arriesgada", "🔴 *ARRIESGADA*", elegir(0.35, 0.55, 3))]


def _bloque_combinada(pr, local, visita):
    tiers = _armar_combinadas(_candidatos(pr, local, visita))
    s = "🔥 *ARMA TU COMBINADA* 🔥\n_Ideas según el modelo, de menos a más riesgo:_\n"
    hay = False
    for clave, nombre, legs in tiers:
        if not legs:
            continue
        hay = True
        prob = 1.0
        for x in legs:
            prob *= x[0]
        g, n = _record_tipo(clave)
        hist = f"  •  histórico `{g}/{n}`" if n >= 5 and len(legs) >= 2 else ""
        conf = min((x[2].get("conf", "baja") for x in legs), key=_RANK.get)
        s += (f"\n{nombre}  •  prob. aprox. `{prob:.0%}`  •  cuota justa `{1 / prob:.2f}`  •  "
              f"{CONF_ICONO[conf]} {conf}{hist}\n")
        for p, t, _spec in legs:
            s += f"   ✅ {t} `{p:.0%}`\n"
    if not hay:
        return "", tiers
    s += "\n💰 _Cuota justa = lo mínimo que debería pagar la casa. Si paga más, hay valor._\n🔞 _Solo +18._"
    return s, tiers


def analizar_partido(texto_usuario, event_id=None, liga=None, meta=None):
    """Devuelve (reporte, error). Si se pasa event_id+liga se analiza ese partido exacto.
    meta (dict opcional) recibe event_id, liga, estado y pick_ids para que la interfaz sepa qué mostró."""
    base = None
    if event_id and liga:
        base = evento_por_id(event_id, liga)
        if not base:
            return None, "⚠️ Ese partido ya no aparece en la lista. No se descontó ningún crédito."
        partes = [normalizar_texto(base["local"]), normalizar_texto(base["visita"])]
    else:
        partes = _partes_busqueda(texto_usuario)
        if not partes or all(len(p) < 3 for p in partes):
            return None, "⚠️ Escribe los equipos así: Equipo A vs Equipo B. No se descontó ningún crédito."
        try:
            base = buscar_espn(partes)
        except Exception as e:
            logging.error(f"ESPN error: {e}")

    vivo = None
    try:
        vivo = buscar_en(partes, cargar_partidos())
    except Exception as e:
        logging.error(f"StatPal error: {e}")

    if not base and vivo:
        try:
            base = buscar_espn([normalizar_texto(vivo["local"]), normalizar_texto(vivo["visita"])])
        except Exception as e:
            logging.error(f"ESPN reintento error: {e}")

    if not base and not vivo:
        return None, ("⚠️ No encontré ese partido (hoy ni en los próximos días). "
                      "No se descontó ningún crédito.\nPrueba: `Equipo A vs Equipo B` con el nombre común del club "
                      "o usa *Partidos de hoy* para elegirlo con un toque.")

    p = dict(base or vivo)
    if base and vivo and vivo["estado"] in ("live", "finished") and _mismo_partido(base, vivo):
        for k in ("gl", "gv", "estado", "minuto", "amarillas", "rojas"):
            p[k] = vivo[k]
        p["fuente"] = "StatPal + ESPN"

    if meta is not None:
        meta.update(event_id=p.get("event_id"), liga=p.get("liga_code"), estado=p["estado"], pick_ids=[],
                    local=p["local"], visita=p["visita"],
                    inicio=p["inicio"].isoformat() if p.get("inicio") else None)

    local, visita, liga_txt = _limpio(p["local"]), _limpio(p["visita"]), _limpio(p["liga"])
    L, V = _corto(local, 16), _corto(visita, 16)
    rep = (f"🤖 *THE ANDROID* • _Centro Inteligente_\n{SEP}\n"
           f"🏆 {liga_txt}\n⚔️ *{local}* 🆚 *{visita}*\n{SEP}\n")

    am = ro = cor_tot = None
    if p["estado"] == "scheduled":
        if p["inicio"]:
            rep += f"🕒 *Inicia:* `{_fmt_inicio(p['inicio'])}`\n"
            cd = _cuenta_regresiva(p["inicio"])
            if cd:
                rep += f"⏳ Empieza en `{cd}`\n"
        else:
            rep += f"🕒 Hora en el feed: `{p['hora_feed']}` _(zona no confirmada)_\n"
    elif p["estado"] in ("live", "finished"):
        gl, gv = p["gl"] or 0, p["gv"] or 0
        rep += ("🔴 *EN VIVO* • `" + str(p["minuto"]) + "`\n" if p["estado"] == "live"
                else "✅ *FINALIZADO*\n")
        rep += f"⚽ *{L}* `{gl}` - `{gv}` *{V}*\n"
        am, ro = p["amarillas"], p["rojas"]
        cor_txt = None
        if p.get("event_id"):
            st = _stats_evento(p["liga_code"], p["event_id"], ttl=30 if p["estado"] == "live" else 3600)
            sl, sv = st.get(str(p["id_local"]), {}), st.get(str(p["id_visita"]), {})
            if am is None and ("amarillas" in sl or "amarillas" in sv):
                am = sl.get("amarillas", 0) + sv.get("amarillas", 0)
            if "corners" in sl or "corners" in sv:
                cor_tot = sl.get("corners", 0) + sv.get("corners", 0)
                cor_txt = f"{sl.get('corners', 0):.0f}-{sv.get('corners', 0):.0f}"
        linea = [f"🟨 `{'s/d' if am is None else int(am)}`", f"🟥 `{'s/d' if ro is None else ro}`"]
        if cor_txt:
            linea.append(f"⛳ `{cor_txt}`")
        rep += "  |  ".join(linea) + "\n"
        if p["estado"] == "live":
            if gl > gv:
                rep += f"📈 Va ganando: *{L}*\n"
            elif gv > gl:
                rep += f"📈 Va ganando: *{V}*\n"
            else:
                rep += "📈 Partido empatado\n"
    else:
        rep += f"ℹ️ Estado: `{_limpio(p['minuto'])}`\n"
    rep += f"🔍 _Fuente: {p['fuente']}_\n"

    if p["estado"] == "finished":
        return rep, None
    en_vivo = p["estado"] == "live"
    if not (p.get("id_local") and p.get("id_visita")):
        return rep + "\nℹ️ _Sin historial suficiente para proyectar este partido._", None

    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f1 = ex.submit(promedios, p["liga_code"], p["id_local"], True)
            f2 = ex.submit(promedios, p["liga_code"], p["id_visita"], False)
            a, b = f1.result(), f2.result()
    except Exception as e:
        logging.error(f"Promedios error: {e}")
        return rep + "\nℹ️ _No pude calcular promedios ahora._", None

    rep += f"\n📋 *FORMA RECIENTE*  _(🟢 ganó 🟡 empató 🔴 perdió)_\n"
    rep += _bloque_equipo(local, a, "🏠", "Como local", "🏟️") + "\n"
    rep += _bloque_equipo(visita, b, "✈️", "Como visita", "🛣️") + "\n"

    frac, gl0, gv0 = 1.0, 0, 0
    if en_vivo:
        mn = _minuto_num(p["minuto"])
        if mn is None:
            return rep + "\nℹ️ _No pude leer el minuto para ajustar la proyección en vivo._", None
        frac = max(0.03, (90 - mn) / 90)
        gl0, gv0 = p["gl"] or 0, p["gv"] or 0
    pr = proyectar(a, b, gl0, gv0, frac, am, cor_tot, en_vivo)
    if not pr:
        return rep + "\nℹ️ _Sin historial suficiente para proyectar este partido._", None

    rep += f"\n{SEP}\n" + _bloque_proyeccion(pr, local, visita, en_vivo)
    comb, tiers = _bloque_combinada(pr, local, visita)
    if meta is not None:
        meta["tiers"] = _items_de_tiers(tiers)
    if comb:
        rep += f"\n\n{SEP}\n" + comb
        if p["estado"] == "scheduled":
            ids = _registrar_tiers(p, tiers)
            if meta is not None:
                meta["pick_ids"] = ids
    return rep, None


# ==================== REGISTRO DE ACIERTOS ====================
_picks_lock = threading.Lock()


def _cargar_picks():
    return bd.picks_todos()


def _clave_pick(event_id, tipo, legs):
    return (str(event_id), tipo, "|".join(sorted(l["texto"] for l in legs)))


def registrar_picks(partido, items, origen):
    """Guarda cada pick/combinada mostrada ANTES del partido (sin duplicar). Devuelve los ids (nuevos y existentes)."""
    if not partido.get("event_id") or not partido.get("inicio"):
        return []
    if partido["inicio"] <= datetime.now(LIMA):
        return []
    ids = []
    with _picks_lock:
        existentes = {_clave_pick(r["event_id"], r["tipo"], r["legs"]): r["id"]
                      for r in bd.picks_por_evento(partido["event_id"])}
        for it in items:
            k = _clave_pick(partido["event_id"], it["tipo"], it["legs"])
            if k in existentes:
                ids.append(existentes[k])
                continue
            rec = {"id": uuid.uuid4().hex[:10], "creado": datetime.now(LIMA).isoformat(),
                   "origen": origen, "event_id": str(partido["event_id"]),
                   "liga_code": partido["liga_code"], "local": partido["local"],
                   "visita": partido["visita"], "inicio": partido["inicio"].isoformat(),
                   "tipo": it["tipo"], "legs": it["legs"], "prob": it["prob"],
                   "estado": "pendiente", "detalle": ""}
            bd.picks_insertar(rec)
            existentes[k] = rec["id"]
            ids.append(rec["id"])
    return ids


def _items_de_tiers(tiers):
    items = []
    for clave, _nombre, legs in tiers:
        if not legs:
            continue
        prob = 1.0
        for x in legs:
            prob *= x[0]
        items.append({"tipo": clave if len(legs) >= 2 else "pick", "prob": prob,
                      "legs": [{"texto": t, "spec": spec, "prob": pp} for pp, t, spec in legs]})
    return items


def _registrar_tiers(p, tiers):
    try:
        return registrar_picks(p, _items_de_tiers(tiers), "analisis")
    except Exception as e:
        logging.error(f"Registro error: {e}")
        return []


def _evaluar_leg(spec, gl, gv, am, cor):
    k = spec["k"]
    if k == "res":
        return {"1": gl > gv, "2": gv > gl, "1X": gl >= gv, "X2": gv >= gl, "12": gl != gv}[spec["v"]]
    if k == "btts":
        return ((gl > 0 and gv > 0) == spec["si"])
    if k == "goles":
        total = gl + gv
    elif k == "amarillas":
        total = am
    elif k == "corners":
        total = cor
    else:
        return None
    if total is None:
        return None
    return total > spec["linea"] if spec["lado"] == "mas" else total < spec["linea"]


def _resultado_evento(r):
    inicio = datetime.fromisoformat(r["inicio"])
    for d in (inicio.date(), inicio.date() + timedelta(days=1), inicio.date() - timedelta(days=1)):
        for p in _scoreboard(r["liga_code"], d):
            if str(p["event_id"]) != r["event_id"]:
                continue
            if p["estado"] != "finished":
                return "pendiente"
            st = _stats_evento(r["liga_code"], r["event_id"], ttl=0)
            ams = [v.get("amarillas") for v in st.values()]
            cors = [v.get("corners") for v in st.values()]
            am = sum(ams) if len(ams) >= 2 and None not in ams else None
            cor = sum(cors) if len(cors) >= 2 and None not in cors else None
            return {"gl": p["gl"], "gv": p["gv"], "am": am, "cor": cor}
    return None


def liquidar_pendientes():
    """Marca ganado/perdido/nulo los picks cuyo partido terminó. Devuelve la lista de picks liquidados."""
    pendientes = bd.picks_pendientes()
    ahora, cache, cambios = datetime.now(LIMA), {}, []
    for r in pendientes:
        inicio = datetime.fromisoformat(r["inicio"])
        if ahora < inicio + timedelta(hours=2):
            continue
        if r["event_id"] not in cache:
            try:
                cache[r["event_id"]] = _resultado_evento(r)
            except Exception as e:
                logging.error(f"Liquidación error: {e}")
                cache[r["event_id"]] = "pendiente"
        res = cache[r["event_id"]]
        if res == "pendiente" or res is None or res["gl"] is None or res["gv"] is None:
            if ahora > inicio + timedelta(days=3):
                cambios.append((r, "nulo", "sin resultado"))
            continue
        evals = [_evaluar_leg(l["spec"], res["gl"], res["gv"], res["am"], res["cor"]) for l in r["legs"]]
        est = "perdido" if any(e is False for e in evals) else "nulo" if any(e is None for e in evals) else "ganado"
        det = f"{res['gl']}-{res['gv']}"
        if res["am"] is not None:
            det += f" 🟨{int(res['am'])}"
        if res["cor"] is not None:
            det += f" ⛳{int(res['cor'])}"
        cambios.append((r, est, det))
    salida = []
    for r, est, det in cambios:
        bd.picks_resolver(r["id"], est, det)
        salida.append(dict(r, estado=est, detalle=det))
    return salida


def _record_tipo(tipo):
    rs = [r for r in _cargar_picks() if r["tipo"] == tipo and r["estado"] in ("ganado", "perdido")]
    return sum(r["estado"] == "ganado" for r in rs), len(rs)


def _linea_record_picks():
    g, n = _record_tipo("pick")
    return f"📈 _Récord real del bot en picks: {g}/{n} ({g / n:.0%})_\n" if n >= 5 else ""


def record_texto():
    db_ = _cargar_picks()
    liq = [r for r in db_ if r["estado"] in ("ganado", "perdido")]
    pend = sum(r["estado"] == "pendiente" for r in db_)
    s = f"📈 *RÉCORD REAL DEL BOT*\n{SEP}\n"
    if not liq:
        return s + (f"Aún no hay picks liquidados ({pend} pendientes).\n"
                    "Cada pick y combinada que el bot muestra antes del partido se guarda y se marca "
                    "ganado o perdido cuando termina. Vuelve en unas horas.")
    desde = min(datetime.fromisoformat(r["creado"]) for r in db_)

    def fila(nombre, rs):
        if not rs:
            return ""
        g = sum(r["estado"] == "ganado" for r in rs)
        prom = sum(r["prob"] for r in rs) / len(rs)
        return f"{nombre}: `{g}/{len(rs)}` ({g / len(rs):.0%}) • el modelo decía `{prom:.0%}`\n"

    por = lambda t: [r for r in liq if r["tipo"] == t]
    s += f"_Desde {desde:%d/%m} • {len(liq)} liquidados • {pend} pendientes_\n\n"
    s += "🎯 *Por tipo*\n"
    s += fila("☝️ Picks simples", por("pick")) + fila("🟢 Combinada segura", por("segura"))
    s += fila("🟡 Combinada equilibrada", por("equilibrada")) + fila("🔴 Combinada arriesgada", por("arriesgada"))

    etiqueta = {"res": "Resultado", "goles": "Goles", "btts": "Goles", "amarillas": "Tarjetas", "corners": "Córners"}
    mercados = {}
    for r in por("pick"):
        mercados.setdefault(etiqueta.get(r["legs"][0]["spec"]["k"], "Otros"), []).append(r)
    if mercados:
        s += "\n📊 *Picks simples por mercado*\n"
        for nombre, rs in sorted(mercados.items(), key=lambda kv: -len(kv[1])):
            s += fila(nombre, rs)

    cal = _cal_actual()
    if cal:
        s += f"\n🧪 _Calibración activa con {cal['n']} picks: el bot corrige sus porcentajes según su propio historial._\n"
    else:
        s += f"\n🧪 _Calibración automática: se activa con {CAL_MIN} picks simples liquidados._\n"

    s += "\n🕒 *Últimos resultados*\n"
    for r in sorted(liq, key=lambda r: r["inicio"], reverse=True)[:6]:
        icono = "✅" if r["estado"] == "ganado" else "❌"
        txt = " + ".join(l["texto"] for l in r["legs"])
        s += (f"{icono} {_corto(r['local'], 12)} vs {_corto(r['visita'], 12)} ({r['detalle']})\n"
              f"    _{_limpio(txt)[:70]}_\n")
    if len(liq) < 30:
        s += "\n⚠️ _Muestra aún pequeña (menos de 30): tómalo como referencia._"
    return s


# ==================== PICKS DEL DÍA ====================
_cache_fijas = {}
CACHE_FIJAS_TTL = 3600
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]


def _picks_partido(p):
    try:
        a = promedios(p["liga_code"], p["id_local"], True)
        b = promedios(p["liga_code"], p["id_visita"], False)
        if not a or not b or a["n"] < 5 or b["n"] < 5:
            return []
        pr = proyectar(a, b)
        if not pr:
            return []
        grupos = {}
        for g, t, prob, spec in _candidatos(pr, p["local"], p["visita"]):
            if 0.72 <= prob < 0.93 and spec["conf"] != "baja":
                grupos.setdefault(g, []).append((prob, t, spec))
        picks = sorted((max(v, key=lambda x: x[0]) for v in grupos.values()),
                       key=lambda x: x[0], reverse=True)[:2]
        return [{"partido": p, "prob": prob, "texto": t, "spec": spec} for prob, t, spec in picks]
    except Exception as e:
        logging.error(f"Picks error: {e}")
        return []


def fijas_del_dia(solo_peru=False, force=False):
    """Devuelve {'texto', 'ids', 'picks'} o None. Usa caché de 1 h; el job la refresca cada 30 min."""
    clave = "pe" if solo_peru else "todo"
    hit = _cache_fijas.get(clave)
    if hit and not force and time.time() - hit[0] < (CACHE_FIJAS_TTL if hit[1] else 600):
        return hit[1]
    hoy, ahora = datetime.now(LIMA).date(), datetime.now(LIMA)
    horas = 96 if solo_peru else 30
    dias = 4 if solo_peru else 2
    candidatos = _scoreboards([hoy + timedelta(days=i) for i in range(dias)])
    lista = [p for p in candidatos
             if p["estado"] == "scheduled" and p["inicio"] and p["id_local"] and p["id_visita"]
             and timedelta(0) < p["inicio"] - ahora < timedelta(hours=horas)
             and (not solo_peru or p["liga_code"] == "per.1")]
    lista.sort(key=lambda p: p["inicio"])
    lista = lista[:12 if solo_peru else 14]
    if not lista:
        _cache_fijas[clave] = (time.time(), None)
        return None
    with ThreadPoolExecutor(max_workers=4) as ex:
        picks = [x for sub in ex.map(_picks_partido, lista) for x in sub]
    if not picks:
        _cache_fijas[clave] = (time.time(), None)
        return None
    picks.sort(key=lambda x: x["prob"], reverse=True)
    picks = picks[:8 if solo_peru else 6]
    ids = []
    for x in picks:
        ids += registrar_picks(x["partido"], [{"tipo": "pick", "prob": x["prob"],
                                               "legs": [{"texto": x["texto"], "spec": x["spec"], "prob": x["prob"]}]}],
                               "picks_pe" if solo_peru else "picks")
    titulo = "🇵🇪 *PICKS LIGA 1 PERÚ* 🇵🇪" if solo_peru else "🏆 *LOS PICKS MÁS FIRMES* 🏆"
    sub = (f"_Partidos de Liga 1 de los próximos {dias} días ({len(lista)} analizados)_" if solo_peru
           else f"_Los que el modelo ve más probables en las próximas {horas} h_")
    s = f"{titulo}\n{sub}\n_Actualizado {datetime.now(LIMA):%H:%M} (Lima)_\n{SEP}\n"
    for i, x in enumerate(picks):
        p = x["partido"]
        justa = 1 / x["prob"]
        conf = x["spec"]["conf"]
        s += (f"\n{NUMS[i]} *{_corto(p['local'], 16)}* 🆚 *{_corto(p['visita'], 16)}*\n"
              f"   🕒 {_fmt_inicio(p['inicio'])}\n"
              f"   ✅ {x['texto']}\n"
              f"   📊 prob. `{x['prob']:.0%}` • cuota justa `{justa:.2f}` • busca cuota ≥ `{justa * 1.05:.2f}`\n"
              f"   🔒 confianza {CONF_ICONO[conf]} {conf}\n")
    s += f"\n{SEP}\n"
    if solo_peru:
        s += "ℹ️ _ESPN no siempre trae córners y tarjetas de Liga 1; en ese caso solo verás picks de goles y resultado._\n"
    s += ("💰 _Compara con la cuota real de tu casa: solo hay valor si paga igual o más que lo indicado. "
          "Usa_ `/valor <cuota> <prob%>`\n")
    s += _linea_record_picks()
    s += "🔞 _Solo +18._"
    res = {"texto": s, "ids": list(dict.fromkeys(ids)), "picks": picks}
    _cache_fijas[clave] = (time.time(), res)
    return res


def resumen_corto(res, solo_peru=False, n=3):
    """Versión breve (solo el cuerpo) para el aviso de la mañana."""
    titulo = "🇵🇪 *Liga 1*" if solo_peru else "🏆 *Picks del día*"
    s = f"{titulo}\n"
    for i, x in enumerate(res["picks"][:n]):
        p = x["partido"]
        s += (f"{NUMS[i]} {_corto(p['local'], 14)} vs {_corto(p['visita'], 14)} • {p['inicio']:%H:%M}\n"
              f"   ✅ {x['texto']} (`{x['prob']:.0%}`)\n")
    return s



# ==================== /GANAR Y AVISO FINAL DE CONSULTAS ====================
_TIER_NOMBRE = {"segura": "🟢 SEGURA", "equilibrada": "🟡 EQUILIBRADA", "arriesgada": "🔴 ARRIESGADA", "pick": "☝️ PICK"}
_ICO = {"ganada": "✅", "perdida": "❌", "juego": "⏳", "sd": "❔"}


def _situacion(c):
    """(partido ESPN fresco, amarillas totales, córners totales) de una consulta guardada."""
    p = evento_por_id(c["event_id"], c["liga_code"])
    if not p:
        return None
    st = _stats_evento(c["liga_code"], c["event_id"], ttl=30 if p["estado"] == "live" else 3600)
    ams = [v.get("amarillas") for v in st.values()]
    cors = [v.get("corners") for v in st.values()]
    am = sum(ams) if len(ams) >= 2 and None not in ams else None
    cor = sum(cors) if len(cors) >= 2 and None not in cors else None
    return p, am, cor


def _estado_leg(spec, gl, gv, am, cor, fin, pre, L, V):
    """(estado, texto de lo que necesita). estado: ganada | perdida | juego | sd."""
    k = spec["k"]
    if fin:
        ev = _evaluar_leg(spec, gl, gv, am, cor)
        if ev is None:
            return "sd", "Sin dato para calificarla"
        tot = {"goles": gl + gv, "amarillas": am, "corners": cor}.get(k)
        det = f"Total final: {tot:g}" if tot is not None else f"Marcador final {gl}-{gv}"
        return ("ganada" if ev else "perdida"), det
    if k in ("goles", "amarillas", "corners"):
        unidad = {"goles": "gol(es)", "amarillas": "amarilla(s)", "corners": "tiro(s) de esquina"}[k]
        total = {"goles": gl + gv, "amarillas": am, "corners": cor}[k]
        tope = math.floor(spec["linea"])
        if pre:
            return "juego", (f"Necesita {tope + 1} o más {unidad} en total" if spec["lado"] == "mas"
                             else f"Debe haber máximo {tope} {unidad} en total")
        if total is None:
            return "sd", f"Sin dato en vivo de {unidad}; se sabrá al final"
        if spec["lado"] == "mas":
            if total > spec["linea"]:
                return "ganada", f"Ya cumplida ({total:g} de {tope + 1} necesarios)"
            return "juego", f"Faltan {tope + 1 - total:g} {unidad} (van {total:g}, debe llegar a {tope + 1})"
        if total > tope:
            return "perdida", f"Ya se pasó ({total:g}; el máximo era {tope})"
        return "juego", f"Pueden caer {tope - total:g} {unidad} más como máximo (van {total:g}, tope {tope})"
    d = gl - gv
    if k == "btts":
        falta = [n for n, g in ((L, gl), (V, gv)) if g == 0]
        if pre:
            return "juego", "Ambos equipos deben anotar" if spec["si"] else "Al menos un equipo debe quedar sin anotar"
        if gl > 0 and gv > 0:
            return ("ganada", "Ambos ya anotaron") if spec["si"] else ("perdida", "Ambos ya anotaron")
        if spec["si"]:
            return "juego", f"Falta que anote {' y '.join(falta)}"
        return "juego", f"Debe quedar sin anotar al menos {' o '.join(falta)}"
    v = spec["v"]
    if pre:
        return "juego", {"1": f"Necesita que gane {L}", "2": f"Necesita que gane {V}",
                         "1X": f"{L} no debe perder", "X2": f"{V} no debe perder",
                         "12": "No debe haber empate"}[v]
    if v == "1":
        t = (f"{L} gana por {d}: debe mantener la ventaja" if d > 0 else
             f"Empatan {gl}-{gv}: {L} necesita marcar y quedar arriba" if d == 0 else
             f"Pierde por {-d}: necesita {-d + 1} gol(es) más que {V} desde ahora")
    elif v == "2":
        t = (f"{V} gana por {-d}: debe mantener la ventaja" if d < 0 else
             f"Empatan {gl}-{gv}: {V} necesita marcar y quedar arriba" if d == 0 else
             f"Pierde por {d}: necesita {d + 1} gol(es) más que {L} desde ahora")
    elif v == "1X":
        t = (f"{L} no pierde ahora: debe no perder" if d >= 0 else
             f"Necesita {-d} gol(es) más que {V} desde ahora para empatar")
    elif v == "X2":
        t = (f"{V} no pierde ahora: debe no perder" if d <= 0 else
             f"Necesita {d} gol(es) más que {L} desde ahora para empatar")
    else:
        t = ("No hay empate ahora: que nadie iguale hasta el final" if d != 0 else
             "Hoy empatan: necesita que alguien marque y gane")
    return "juego", t


def ganar_texto(c):
    """Explica, para una consulta guardada, qué necesita cada selección para ganar según el estado actual."""
    L, V = _corto(c["local"], 16), _corto(c["visita"], 16)
    s = f"🎯 *{L} vs {V}*\n"
    sit = _situacion(c)
    if not sit:
        return s + "ℹ️ _No pude leer el partido ahora, intenta en un minuto._"
    p, am, cor = sit
    gl, gv = p["gl"] or 0, p["gv"] or 0
    fin, pre = p["estado"] == "finished", p["estado"] == "scheduled"
    if pre:
        s += f"🕒 Aún no empieza: `{_fmt_inicio(p['inicio'])}`\n" if p.get("inicio") else "🕒 Aún no empieza\n"
    elif fin:
        s += f"✅ *Final* `{gl}-{gv}`"
    else:
        s += f"🔴 *En vivo* `{p['minuto']}` • `{gl}-{gv}`"
    if not pre:
        s += (f"  🟨 `{'s/d' if am is None else int(am)}`  ⛳ `{'s/d' if cor is None else int(cor)}`\n")
    if not c["tiers"]:
        return s + "\n_Tu consulta no tenía selecciones armadas para este partido._"
    for t in c["tiers"]:
        res = [_estado_leg(l["spec"], gl, gv, am, cor, fin, pre, L, V) for l in t["legs"]]
        sts = [r[0] for r in res]
        if "perdida" in sts:
            tot = "❌ perdida"
        elif all(x == "ganada" for x in sts):
            tot = "✅ ganada" if fin else "✅ asegurada"
        else:
            tot = "⏳ en juego"
        s += f"\n{_TIER_NOMBRE.get(t['tipo'], t['tipo'])} • {tot}\n"
        for l, (stt, txt) in zip(t["legs"], res):
            s += f"   {_ICO[stt]} {_limpio(l['texto'])}\n      _{_limpio(txt)}_\n"
    s += "\n_Se actualiza cada vez que escribes_ /ganar"
    return s


def liquidar_consultas():
    """Califica las consultas guardadas de los usuarios cuando el partido terminó. Devuelve las liquidadas."""
    ahora, cache, salida = datetime.now(LIMA), {}, []
    for c in bd.consultas_pendientes():
        try:
            inicio = datetime.fromisoformat(c["inicio"])
        except Exception:
            continue
        if ahora < inicio + timedelta(hours=2):
            continue
        ev = c["event_id"]
        if ev not in cache:
            try:
                cache[ev] = _resultado_evento({"liga_code": c["liga_code"], "event_id": ev, "inicio": c["inicio"]})
            except Exception as e:
                logging.error(f"Liquidar consulta: {e}")
                cache[ev] = "pendiente"
        res = cache[ev]
        if res == "pendiente" or res is None or res["gl"] is None or res["gv"] is None:
            if ahora > inicio + timedelta(days=3):
                bd.consulta_resolver(c["id"], "nula", {"gl": None, "gv": None, "tiers": []})
            continue
        tiers_res = []
        for t in c["tiers"]:
            evals = [_evaluar_leg(l["spec"], res["gl"], res["gv"], res["am"], res["cor"]) for l in t["legs"]]
            est = "perdida" if any(e is False for e in evals) else "nula" if any(e is None for e in evals) else "ganada"
            tiers_res.append({"tipo": t["tipo"], "estado": est,
                              "legs": [{"texto": l["texto"], "ok": e} for l, e in zip(t["legs"], evals)]})
        resultado = {"gl": res["gl"], "gv": res["gv"], "am": res["am"], "cor": res["cor"], "tiers": tiers_res}
        bd.consulta_resolver(c["id"], "finalizada", resultado)
        salida.append(dict(c, resultado=resultado, estado="finalizada"))
    return salida
