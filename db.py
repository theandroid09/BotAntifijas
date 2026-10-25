"""Capa de datos SQLite de The Android: usuarios, créditos, números, picks, recargas, métricas."""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "android.db"))

CREDITOS_INICIALES = 5.0
BONUS_DIARIO = 2.0
BONUS_MIN_EDAD = 24 * 3600     # la cuenta debe tener 24 h antes de poder reclamar el bonus
REF_BONUS = 1.5                # créditos para quien invita (cuando el invitado hace su 1ª compra/consulta)
REF_MAX_DIA = 10               # tope de premios por referido al día, por invitador

_lock = threading.RLock()
_conn = None


def conn():
    global _conn
    with _lock:
        if _conn is None:
            c = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=5000")
            _init(c)
            _conn = c
        return _conn


def _init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        uid TEXT PRIMARY KEY, nombre TEXT DEFAULT '', creditos REAL NOT NULL DEFAULT 0,
        compras INTEGER NOT NULL DEFAULT 0, consultas INTEGER NOT NULL DEFAULT 0,
        ultimo_bonus REAL NOT NULL DEFAULT 0, creado REAL NOT NULL, ultimo_activo REAL NOT NULL,
        referido_por TEXT, ref_pagado INTEGER NOT NULL DEFAULT 0, avisos INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS movimientos(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, uid TEXT NOT NULL,
        monto REAL NOT NULL, motivo TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_mov_ts ON movimientos(ts);
    CREATE TABLE IF NOT EXISTS eventos(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, uid TEXT NOT NULL, evento TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_ev_ts ON eventos(ts);
    CREATE TABLE IF NOT EXISTS numeros(
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, tzid TEXT NOT NULL, phone TEXT,
        servicio TEXT, costo REAL NOT NULL, creado REAL NOT NULL, estado TEXT NOT NULL DEFAULT 'activo',
        codigo TEXT);
    CREATE INDEX IF NOT EXISTS ix_num_uid ON numeros(uid, estado);
    CREATE TABLE IF NOT EXISTS recargas(
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, paquete INTEGER, monto_pen REAL,
        creditos REAL NOT NULL, file_unique TEXT UNIQUE, file_id TEXT, estado TEXT NOT NULL DEFAULT 'pendiente',
        creado REAL NOT NULL, resuelto REAL);
    CREATE TABLE IF NOT EXISTS picks(
        id TEXT PRIMARY KEY, estado TEXT NOT NULL, inicio TEXT, event_id TEXT, data TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_picks_estado ON picks(estado);
    CREATE INDEX IF NOT EXISTS ix_picks_event ON picks(event_id);
    CREATE TABLE IF NOT EXISTS pick_vistas(
        uid TEXT NOT NULL, pick_id TEXT NOT NULL, PRIMARY KEY(uid, pick_id));
    CREATE TABLE IF NOT EXISTS consultas(
        id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, creado REAL NOT NULL,
        event_id TEXT NOT NULL, liga_code TEXT, local TEXT, visita TEXT, inicio TEXT,
        tiers TEXT NOT NULL DEFAULT '[]', estado TEXT NOT NULL DEFAULT 'pendiente', resultado TEXT NOT NULL DEFAULT '');
    CREATE INDEX IF NOT EXISTS ix_cons_uid ON consultas(uid, creado);
    CREATE INDEX IF NOT EXISTS ix_cons_estado ON consultas(estado);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    """)


@contextmanager
def _tx():
    with _lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def _mov(c, uid, monto, motivo):
    c.execute("INSERT INTO movimientos(ts,uid,monto,motivo) VALUES(?,?,?,?)", (time.time(), str(uid), monto, motivo))


# ---------------- migración desde los JSON antiguos ----------------
def migrar_json():
    """Importa usuarios_db.json y picks_db.json una sola vez y los renombra a .migrado."""
    f_users = os.path.join(BASE_DIR, "usuarios_db.json")
    if os.path.exists(f_users):
        try:
            data = json.load(open(f_users, encoding="utf-8"))
            now = time.time()
            with _tx() as c:
                for uid, d in data.items():
                    c.execute("""INSERT OR IGNORE INTO users(uid,creditos,compras,consultas,ultimo_bonus,creado,ultimo_activo)
                                 VALUES(?,?,?,?,?,?,?)""",
                              (str(uid), float(d.get("creditos", 0)), int(d.get("compras", 0)),
                               int(d.get("consultas_apuestas", 0)), float(d.get("ultimo_bonus", 0)),
                               now - 7 * 86400, now))
            os.replace(f_users, f_users + ".migrado")
        except Exception as e:
            print(f"⚠️ No pude migrar usuarios_db.json: {e}")
    f_picks = os.path.join(BASE_DIR, "picks_db.json")
    if os.path.exists(f_picks):
        try:
            data = json.load(open(f_picks, encoding="utf-8"))
            with _tx() as c:
                for r in data:
                    c.execute("INSERT OR IGNORE INTO picks(id,estado,inicio,event_id,data) VALUES(?,?,?,?,?)",
                              (r["id"], r["estado"], r.get("inicio"), r.get("event_id"), json.dumps(r, ensure_ascii=False)))
            os.replace(f_picks, f_picks + ".migrado")
        except Exception as e:
            print(f"⚠️ No pude migrar picks_db.json: {e}")


# ---------------- usuarios y créditos ----------------
def usuario(uid, nombre="", ref=None, admin=False):
    uid = str(uid)
    with _tx() as c:
        row = c.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone()
        now = time.time()
        if row is None:
            ref_ok = None
            if ref and str(ref) != uid and c.execute("SELECT 1 FROM users WHERE uid=?", (str(ref),)).fetchone():
                ref_ok = str(ref)
            ini = 100.0 if admin else CREDITOS_INICIALES
            c.execute("INSERT INTO users(uid,nombre,creditos,creado,ultimo_activo,referido_por) VALUES(?,?,?,?,?,?)",
                      (uid, nombre, ini, now, now, ref_ok))
            _mov(c, uid, ini, "registro")
        else:
            c.execute("UPDATE users SET ultimo_activo=?, nombre=? WHERE uid=?", (now, nombre or row["nombre"], uid))
        return dict(c.execute("SELECT * FROM users WHERE uid=?", (uid,)).fetchone())


def get_usuario(uid):
    with _lock:
        r = conn().execute("SELECT * FROM users WHERE uid=?", (str(uid),)).fetchone()
        return dict(r) if r else None


def creditos(uid):
    u = get_usuario(uid)
    return round(u["creditos"], 2) if u else 0.0


def cobrar(uid, monto, motivo):
    """Descuenta de forma atómica. False si no alcanza el saldo."""
    uid = str(uid)
    with _tx() as c:
        cur = c.execute("UPDATE users SET creditos=creditos-? WHERE uid=? AND creditos>=?", (monto, uid, monto - 1e-9))
        if cur.rowcount != 1:
            return False
        _mov(c, uid, -monto, motivo)
        if motivo.startswith("consulta"):
            c.execute("UPDATE users SET consultas=consultas+1 WHERE uid=?", (uid,))
        elif motivo.startswith("numero"):
            c.execute("UPDATE users SET compras=compras+1 WHERE uid=?", (uid,))
        return True


def abonar(uid, monto, motivo):
    uid = str(uid)
    with _tx() as c:
        c.execute("""INSERT INTO users(uid,creditos,creado,ultimo_activo) VALUES(?,?,?,?)
                     ON CONFLICT(uid) DO UPDATE SET creditos=creditos+excluded.creditos""",
                  (uid, monto, time.time(), time.time()))
        _mov(c, uid, monto, motivo)


def reclamar_bonus(uid):
    """('ok', None) | ('nuevo', segundos_que_faltan) | ('ya', segundos_que_faltan)."""
    uid = str(uid)
    now = time.time()
    with _tx() as c:
        u = c.execute("SELECT creado, ultimo_bonus FROM users WHERE uid=?", (uid,)).fetchone()
        if u is None:
            return "nuevo", BONUS_MIN_EDAD
        if now - u["creado"] < BONUS_MIN_EDAD:
            return "nuevo", BONUS_MIN_EDAD - (now - u["creado"])
        if now - u["ultimo_bonus"] < 86400:
            return "ya", 86400 - (now - u["ultimo_bonus"])
        c.execute("UPDATE users SET creditos=creditos+?, ultimo_bonus=? WHERE uid=?", (BONUS_DIARIO, now, uid))
        _mov(c, uid, BONUS_DIARIO, "bonus")
        return "ok", None


def premiar_referido(uid):
    """Si el usuario fue invitado y es su primera acción de pago, premia al invitador. Devuelve el uid del invitador."""
    uid = str(uid)
    with _tx() as c:
        u = c.execute("SELECT referido_por, ref_pagado FROM users WHERE uid=?", (uid,)).fetchone()
        if not u or not u["referido_por"] or u["ref_pagado"]:
            return None
        inv = u["referido_por"]
        c.execute("UPDATE users SET ref_pagado=1 WHERE uid=?", (uid,))
        hoy = c.execute("SELECT COUNT(*) FROM movimientos WHERE uid=? AND motivo='referido' AND ts>?",
                        (inv, time.time() - 86400)).fetchone()[0]
        if hoy >= REF_MAX_DIA:
            return None
        c.execute("UPDATE users SET creditos=creditos+? WHERE uid=?", (REF_BONUS, inv))
        _mov(c, inv, REF_BONUS, "referido")
        return inv


def ref_stats(uid):
    with _lock:
        c = conn()
        tot = c.execute("SELECT COUNT(*) FROM users WHERE referido_por=?", (str(uid),)).fetchone()[0]
        pag = c.execute("SELECT COUNT(*) FROM users WHERE referido_por=? AND ref_pagado=1", (str(uid),)).fetchone()[0]
        return tot, pag


def alternar_avisos(uid):
    with _tx() as c:
        c.execute("UPDATE users SET avisos=1-avisos WHERE uid=?", (str(uid),))
        return bool(c.execute("SELECT avisos FROM users WHERE uid=?", (str(uid),)).fetchone()[0])


def todos_los_uids(solo_avisos=False):
    with _lock:
        q = "SELECT uid FROM users" + (" WHERE avisos=1" if solo_avisos else "")
        return [r[0] for r in conn().execute(q).fetchall()]


# ---------------- números virtuales ----------------
def numero_guardar(uid, tzid, phone, servicio, costo):
    with _tx() as c:
        cur = c.execute("INSERT INTO numeros(uid,tzid,phone,servicio,costo,creado) VALUES(?,?,?,?,?,?)",
                        (str(uid), str(tzid), phone, servicio, costo, time.time()))
        return cur.lastrowid


def numeros_activos(uid, minutos=60, limite=3):
    with _lock:
        rows = conn().execute("""SELECT * FROM numeros WHERE uid=? AND estado='activo' AND creado>?
                                 ORDER BY creado DESC LIMIT ?""",
                              (str(uid), time.time() - minutos * 60, limite)).fetchall()
        return [dict(r) for r in rows]


def numero_marcar(num_id, estado, codigo=None):
    """True solo la primera vez que cambia desde 'activo' (sirve para reembolsar una única vez)."""
    with _tx() as c:
        cur = c.execute("UPDATE numeros SET estado=?, codigo=COALESCE(?,codigo) WHERE id=? AND estado='activo'",
                        (estado, codigo, num_id))
        return cur.rowcount == 1


# ---------------- recargas (Yape / Plin) ----------------
def recarga_crear(uid, paquete, monto_pen, creditos_, file_unique, file_id):
    try:
        with _tx() as c:
            cur = c.execute("""INSERT INTO recargas(uid,paquete,monto_pen,creditos,file_unique,file_id,creado)
                               VALUES(?,?,?,?,?,?,?)""",
                            (str(uid), paquete, monto_pen, creditos_, file_unique, file_id, time.time()))
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None      # la misma captura ya se había enviado


def recarga_resolver(rid, estado):
    with _tx() as c:
        cur = c.execute("UPDATE recargas SET estado=?, resuelto=? WHERE id=? AND estado='pendiente'",
                        (estado, time.time(), rid))
        if cur.rowcount != 1:
            return None
        r = dict(c.execute("SELECT * FROM recargas WHERE id=?", (rid,)).fetchone())
        if estado == "aprobada":
            c.execute("UPDATE users SET creditos=creditos+? WHERE uid=?", (r["creditos"], r["uid"]))
            _mov(c, r["uid"], r["creditos"], "recarga")
        return r


def recargas_pendientes(limite=10):
    with _lock:
        return [dict(r) for r in conn().execute(
            "SELECT * FROM recargas WHERE estado='pendiente' ORDER BY creado LIMIT ?", (limite,)).fetchall()]


# ---------------- picks ----------------
def picks_insertar(rec):
    with _tx() as c:
        c.execute("INSERT OR IGNORE INTO picks(id,estado,inicio,event_id,data) VALUES(?,?,?,?,?)",
                  (rec["id"], rec["estado"], rec.get("inicio"), rec.get("event_id"), json.dumps(rec, ensure_ascii=False)))


def _rows_picks(rows):
    out = []
    for r in rows:
        d = json.loads(r["data"])
        d["estado"] = r["estado"]
        out.append(d)
    return out


def picks_todos():
    with _lock:
        return _rows_picks(conn().execute("SELECT data, estado FROM picks").fetchall())


def picks_por_evento(event_id):
    with _lock:
        return _rows_picks(conn().execute("SELECT data, estado FROM picks WHERE event_id=?", (str(event_id),)).fetchall())


def picks_pendientes():
    with _lock:
        return _rows_picks(conn().execute("SELECT data, estado FROM picks WHERE estado='pendiente'").fetchall())


def picks_resolver(pid, estado, detalle):
    with _tx() as c:
        row = c.execute("SELECT data FROM picks WHERE id=?", (pid,)).fetchone()
        if not row:
            return
        d = json.loads(row["data"])
        d["estado"], d["detalle"] = estado, detalle
        c.execute("UPDATE picks SET estado=?, data=? WHERE id=?", (estado, json.dumps(d, ensure_ascii=False), pid))


def vistas_registrar(uid, pick_ids):
    if not pick_ids:
        return
    with _tx() as c:
        c.executemany("INSERT OR IGNORE INTO pick_vistas(uid,pick_id) VALUES(?,?)", [(str(uid), p) for p in pick_ids])


def vistas_de(pick_id):
    with _lock:
        return [r[0] for r in conn().execute(
            """SELECT v.uid FROM pick_vistas v JOIN users u ON u.uid=v.uid
               WHERE v.pick_id=? AND u.avisos=1""", (pick_id,)).fetchall()]


# ---------------- kv y métricas ----------------
def kv_get(k, default=None):
    with _lock:
        r = conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default


def kv_set(k, v):
    with _tx() as c:
        c.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


def log_evento(uid, evento):
    try:
        with _tx() as c:
            c.execute("INSERT INTO eventos(ts,uid,evento) VALUES(?,?,?)", (time.time(), str(uid), evento))
    except Exception:
        pass


def metricas_texto():
    now = time.time()
    d7, d1 = now - 7 * 86400, now - 86400
    with _lock:
        c = conn()
        q = lambda sql, *a: c.execute(sql, a).fetchall()
        total = q("SELECT COUNT(*) FROM users")[0][0]
        nuevos = q("""SELECT date(creado,'unixepoch','-5 hours') d, COUNT(*) FROM users
                      WHERE creado>? GROUP BY d ORDER BY d DESC""", d7)
        act1 = q("SELECT COUNT(DISTINCT uid) FROM eventos WHERE ts>?", d1)[0][0]
        act7 = q("SELECT COUNT(DISTINCT uid) FROM eventos WHERE ts>?", d7)[0][0]
        vuelven = q("""SELECT COUNT(*) FROM (SELECT uid FROM eventos WHERE ts>?
                       GROUP BY uid HAVING COUNT(DISTINCT date(ts,'unixepoch','-5 hours'))>=2)""", now - 14 * 86400)[0][0]
        top = q("""SELECT evento, COUNT(*) n FROM eventos WHERE ts>? GROUP BY evento ORDER BY n DESC LIMIT 8""", d7)
        gastos = q("""SELECT motivo, -SUM(monto) FROM movimientos WHERE ts>? AND monto<0
                      GROUP BY motivo ORDER BY 2 DESC""", d7)
        ingresos = q("""SELECT motivo, SUM(monto) FROM movimientos WHERE ts>? AND monto>0 AND motivo!='registro'
                        GROUP BY motivo ORDER BY 2 DESC""", d7)
        pend = q("SELECT COUNT(*) FROM recargas WHERE estado='pendiente'")[0][0]
        rec_ok = q("SELECT COALESCE(SUM(monto_pen),0) FROM recargas WHERE estado='aprobada' AND resuelto>?", d7)[0][0]
    s = f"📊 *MÉTRICAS*\n👥 Usuarios totales: `{total}`\n"
    s += f"🟢 Activos 24 h: `{act1}` • 7 días: `{act7}` • volvieron (≥2 días, 14 d): `{vuelven}`\n"
    s += "\n🆕 *Nuevos por día*\n" + ("\n".join(f"  {d[5:]}: `{n}`" for d, n in nuevos) or "  —") + "\n"
    s += "\n🔘 *Más usado (7 d)*\n" + ("\n".join(f"  {e}: `{n}`" for e, n in top) or "  —") + "\n"
    s += "\n🪙 *Créditos consumidos (7 d)*\n" + ("\n".join(f"  {m}: `{v:.1f}`" for m, v in gastos) or "  —") + "\n"
    s += "\n➕ *Créditos entregados (7 d)*\n" + ("\n".join(f"  {m}: `{v:.1f}`" for m, v in ingresos) or "  —") + "\n"
    s += f"\n💵 Recargas aprobadas (7 d): `S/ {rec_ok:.2f}` • pendientes: `{pend}`"
    return s.replace("_", " ")


# ---------------- historial de consultas del usuario ----------------
def _cons(r):
    d = dict(r)
    d["tiers"] = json.loads(d["tiers"] or "[]")
    d["resultado"] = json.loads(d["resultado"]) if d["resultado"] else None
    return d


def consulta_guardar(uid, meta, finalizada=False):
    """Una fila por usuario y partido: si ya hay una pendiente, se actualiza con lo último que vio."""
    from datetime import datetime, timedelta, timezone
    inicio = meta.get("inicio") or datetime.now(timezone(timedelta(hours=-5))).isoformat()
    tiers = json.dumps(meta.get("tiers") or [], ensure_ascii=False)
    with _tx() as c:
        row = c.execute("SELECT id FROM consultas WHERE uid=? AND event_id=? AND estado='pendiente'",
                        (str(uid), str(meta["event_id"]))).fetchone()
        if row and not finalizada:
            c.execute("UPDATE consultas SET tiers=?, creado=? WHERE id=?", (tiers, time.time(), row["id"]))
            return row["id"]
        cur = c.execute("""INSERT INTO consultas(uid,creado,event_id,liga_code,local,visita,inicio,tiers,estado)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (str(uid), time.time(), str(meta["event_id"]), meta.get("liga"), meta.get("local"),
                         meta.get("visita"), inicio, tiers, "finalizada" if finalizada else "pendiente"))
        return cur.lastrowid


def consultas_pendientes():
    with _lock:
        return [_cons(r) for r in conn().execute("SELECT * FROM consultas WHERE estado='pendiente'").fetchall()]


def consulta_resolver(cid, estado, resultado):
    with _tx() as c:
        c.execute("UPDATE consultas SET estado=?, resultado=? WHERE id=?",
                  (estado, json.dumps(resultado, ensure_ascii=False), cid))


def consultas_de(uid, limite=10, solo_pendientes=False):
    with _lock:
        q = "SELECT * FROM consultas WHERE uid=?" + (" AND estado='pendiente'" if solo_pendientes else "")
        return [_cons(r) for r in conn().execute(q + " ORDER BY creado DESC LIMIT ?", (str(uid), limite)).fetchall()]


def consulta_por_evento(uid, event_id):
    with _lock:
        r = conn().execute("SELECT * FROM consultas WHERE uid=? AND event_id=? ORDER BY creado DESC LIMIT 1",
                           (str(uid), str(event_id))).fetchone()
        return _cons(r) if r else None
