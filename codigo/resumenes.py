"""Resúmenes: diario, semanal y recap de lo que te perdiste mientras el bot estaba apagado."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from almacenamiento import (
    ahora,
    elementos_desde,
    elementos_sin_enviar,
    guardar_metadato,
    leer_metadato,
    marcar_enviado,
    obtener_fuentes,
)
from avisos import enviar_aviso
from clasificador import ETIQUETAS, normalizar
from duplicados import RANGO_TIPO

NIVELES = ["critica", "alta", "media", "baja"]
ENCABEZADOS = {
    "critica": "🔴 Crítica",
    "alta": "🟠 Alta",
    "media": "🟡 Media",
    "baja": "⚪ Baja",
}

LIMITE_PARTE = 3800          # Discord admite 4096 caracteres por tarjeta; dejamos margen
MINUTOS_MINIMOS_RECAP = 120  # si estuvo apagado menos tiempo que esto, no se manda recap

DIAS_SEMANA = {
    "lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3,
    "viernes": 4, "sabado": 5, "domingo": 6,
}


# ---------- Formato del texto ----------

def _rango_importancia(importancia):
    return NIVELES.index(importancia) if importancia in NIVELES else len(NIVELES)


def _limpiar(texto, maximo):
    """Una sola línea, sin corchetes (romperían los enlaces de Discord) y con longitud máxima."""
    texto = " ".join((texto or "").split())
    texto = texto.replace("[", "(").replace("]", ")")
    if len(texto) > maximo:
        texto = texto[: maximo - 1].rstrip() + "…"
    return texto


def _enlace(titulo, url, maximo=200):
    texto = _limpiar(titulo, maximo)
    if not url:
        return texto
    return f"[{texto}]({url.replace(')', '%29')})"


def _info_fuentes(conexion):
    return {
        f["id"]: {"nombre": f["nombre"], "tipo": f["tipo"], "peso": f["peso"]}
        for f in obtener_fuentes(conexion)
    }


def _agrupar(filas, fuentes):
    """Junta las menciones de la misma noticia (mismo grupo) y las ordena por importancia."""
    grupos = {}
    for f in filas:
        info = fuentes.get(f["fuente_id"], {"nombre": f["fuente_id"], "tipo": "comunidad", "peso": 0})
        miembro = {"fila": f, "nombre": info["nombre"], "tipo": info["tipo"], "peso": info["peso"]}
        grupos.setdefault(f["grupo"], []).append(miembro)

    resultado = []
    for miembros in grupos.values():
        # La primera mención es la de la fuente más fiable (tipo y después peso)
        miembros.sort(key=lambda m: (
            RANGO_TIPO.get(m["tipo"], 9),
            -(m["peso"] or 0),
            m["fila"]["detectado_en"],
        ))
        importancia = min((m["fila"]["importancia"] for m in miembros), key=_rango_importancia)
        detectado = min(m["fila"]["detectado_en"] for m in miembros)
        resultado.append({"importancia": importancia, "miembros": miembros, "detectado": detectado})

    resultado.sort(key=lambda g: (_rango_importancia(g["importancia"]), g["detectado"]))
    return resultado


def _bloque_grupo(grupo):
    """Texto de una noticia: título con enlace, fuente con su etiqueta, resumen y otras menciones."""
    principal = grupo["miembros"][0]
    f = principal["fila"]
    etiqueta = ETIQUETAS.get(principal["tipo"], ETIQUETAS["comunidad"])
    idioma = (f["idioma"] or "?").upper()

    lineas = [
        f"**{_enlace(f['titulo'], f['url'])}**",
        f"{etiqueta} · {principal['nombre']} · {idioma}",
    ]
    resumen = _limpiar(f["resumen"], 220)
    if resumen:
        lineas.append(resumen)

    otros = grupo["miembros"][1:]
    if otros:
        menciones = [
            f"{_enlace(m['fila']['titulo'], m['fila']['url'], 60)} "
            f"({m['nombre']}, {(m['fila']['idioma'] or '?').upper()})"
            for m in otros
        ]
        lineas.append("También en: " + "; ".join(menciones))
    return "\n".join(lineas)


def _bloques_por_nivel(grupos):
    bloques = []
    nivel_actual = None
    for g in grupos:
        texto = _bloque_grupo(g)
        if g["importancia"] != nivel_actual:
            nivel_actual = g["importancia"]
            texto = f"**{ENCABEZADOS.get(nivel_actual, nivel_actual)}**\n{texto}"
        bloques.append(texto)
    return bloques


def _trocear(bloques, limite=LIMITE_PARTE):
    """Reparte los bloques en partes que quepan en una tarjeta de Discord."""
    partes = []
    actual = ""
    for bloque in bloques:
        if len(bloque) > limite:
            bloque = bloque[: limite - 1].rstrip() + "…"
        if not actual:
            actual = bloque
        elif len(actual) + 2 + len(bloque) <= limite:
            actual += "\n\n" + bloque
        else:
            partes.append(actual)
            actual = bloque
    if actual:
        partes.append(actual)
    return partes


def partes_resumen(conexion, filas):
    """Construye el texto de un resumen.

    Devuelve (partes, hay_preventas). Las preventas van primero, destacadas;
    después las noticias, de más a menos importantes.
    """
    fuentes = _info_fuentes(conexion)
    preventas = [f for f in filas if f["categoria"] == "preventa"]
    noticias = [f for f in filas if f["categoria"] != "preventa"]

    bloques = []
    for i, grupo in enumerate(_agrupar(preventas, fuentes)):
        texto = _bloque_grupo(grupo)
        if i == 0:
            texto = "**🚨 PREVENTAS Y ACCESOS DETECTADOS**\n" + texto
        bloques.append(texto)
    bloques += _bloques_por_nivel(_agrupar(noticias, fuentes))

    return _trocear(bloques), bool(preventas)


def _enviar_partes(partes, titulo, canal, hay_preventas, enviar):
    for i, parte in enumerate(partes, 1):
        titulo_parte = titulo if len(partes) == 1 else f"{titulo} ({i}/{len(partes)})"
        tipo = "preventa" if (hay_preventas and i == 1) else "noticia"
        enviar(canal=canal, titulo=titulo_parte, descripcion=parte, tipo=tipo)


# ---------- Cuándo toca enviar ----------

def _zona(config):
    return ZoneInfo(config.get("resumenes", {}).get("zona_horaria", "Europe/Madrid"))


def _local(config, ahora_utc=None):
    return (ahora_utc or datetime.now(timezone.utc)).astimezone(_zona(config))


def _dia_semana(nombre):
    clave = normalizar(nombre).strip()
    if clave not in DIAS_SEMANA:
        raise ValueError(f"dia_semanal no válido: «{nombre}». Usa lunes, martes, miércoles...")
    return DIAS_SEMANA[clave]


def resumen_debido(conexion, config, ahora_utc=None):
    """Dice qué resumen toca enviar ahora: "semanal", "diario" o None."""
    cfg = config.get("resumenes", {})
    local = _local(config, ahora_utc)
    hoy = local.date().isoformat()

    hora, minuto = cfg.get("hora_diaria", "09:00").split(":")
    if (local.hour, local.minute) < (int(hora), int(minuto)):
        return None

    es_dia_semanal = bool(cfg.get("semanal")) and local.weekday() == _dia_semana(
        cfg.get("dia_semanal", "lunes")
    )
    if es_dia_semanal:
        # El día del semanal no se manda el diario: el semanal ya lo cubre
        return "semanal" if leer_metadato(conexion, "ultimo_resumen_semanal") != hoy else None

    if cfg.get("diario") and leer_metadato(conexion, "ultimo_resumen_diario") != hoy:
        return "diario"
    return None


def enviar_resumen(conexion, config, tipo, ahora_utc=None, enviar=enviar_aviso):
    """Envía el resumen diario o semanal al canal de noticias. Devuelve cuántas noticias llevaba."""
    ahora_utc = ahora_utc or datetime.now(timezone.utc)
    local = _local(config, ahora_utc)
    hoy = local.date().isoformat()

    if tipo == "semanal":
        desde = (ahora_utc - timedelta(days=7)).isoformat(timespec="seconds")
        filas = [f for f in elementos_desde(conexion, desde) if f["categoria"] == "noticia"]
        titulo = f"📅 Resumen semanal · semana hasta el {local.strftime('%d/%m/%Y')}"
    elif tipo == "diario":
        filas = list(elementos_sin_enviar(conexion, "noticia"))
        titulo = f"📰 Resumen diario · {local.strftime('%d/%m/%Y')}"
    else:
        raise ValueError(f"Tipo de resumen no válido: {tipo}")

    if filas:
        partes, hay_preventas = partes_resumen(conexion, filas)
        _enviar_partes(partes, titulo, "noticias", hay_preventas, enviar)
        for f in filas:
            marcar_enviado(conexion, f["id"])

    # Solo se llega aquí si el envío no falló: así un error se reintenta en la siguiente pasada
    guardar_metadato(conexion, "ultimo_resumen_diario", hoy)
    if tipo == "semanal":
        guardar_metadato(conexion, "ultimo_resumen_semanal", hoy)
    return len(filas)


# ---------- Recap tras estar apagado ----------

def formatear_duracion(delta):
    minutos = int(delta.total_seconds() // 60)
    dias, resto = divmod(minutos, 1440)
    horas, mins = divmod(resto, 60)
    if dias:
        return f"{dias} d {horas} h"
    if horas:
        return f"{horas} h {mins} min"
    return f"{mins} min"


def enviar_recap(conexion, ultima_ejecucion, ahora_utc=None,
                 minimo_minutos=MINUTOS_MINIMOS_RECAP, enviar=enviar_aviso):
    """Envía lo detectado desde la última ejecución, si el bot estuvo apagado un rato.

    Hay que llamarlo DESPUÉS de la primera revisión al arrancar (para que ya se hayan
    detectado las novedades) y ANTES de registrar la ejecución actual.
    Devuelve cuántos elementos incluyó.
    """
    if not ultima_ejecucion:
        return 0
    ahora_utc = ahora_utc or datetime.now(timezone.utc)
    try:
        desde = datetime.fromisoformat(ultima_ejecucion)
    except ValueError:
        return 0
    if desde.tzinfo is None:
        desde = desde.replace(tzinfo=timezone.utc)

    hueco = ahora_utc - desde
    if hueco < timedelta(minutes=minimo_minutos):
        return 0

    duracion = formatear_duracion(hueco)
    filas = [f for f in elementos_desde(conexion, ultima_ejecucion) if not f["enviado"]]
    if not filas:
        enviar(
            canal="estado",
            titulo="⏪ Recap al arrancar",
            descripcion=f"El bot estuvo apagado {duracion}. No hay novedades pendientes de contarte.",
            tipo="estado",
        )
        return 0

    partes, hay_preventas = partes_resumen(conexion, filas)
    _enviar_partes(partes, f"⏪ Recap: lo que te perdiste ({duracion} apagado)",
                   "noticias", hay_preventas, enviar)
    for f in filas:
        marcar_enviado(conexion, f["id"])
    return len(filas)


def registrar_ejecucion(conexion):
    """Anota que el bot acaba de ejecutarse (sirve para calcular el tiempo apagado)."""
    guardar_metadato(conexion, "ultima_ejecucion", ahora())


# ---------- Pruebas: python codigo/resumenes.py ----------

if __name__ == "__main__":
    from almacenamiento import conectar, guardar_elemento, registrar_fuentes

    enviados = []

    def falso(canal, titulo, descripcion, tipo="estado", mencion=None):
        enviados.append({"canal": canal, "titulo": titulo, "texto": descripcion, "tipo": tipo})

    FUENTES = [
        {"id": "oficial", "nombre": "Web oficial", "tipo": "oficial", "peso_inicial": 95},
        {"id": "medio", "nombre": "Medio de prueba", "tipo": "medio", "peso_inicial": 60},
        {"id": "tienda", "nombre": "Tienda de prueba", "tipo": "tienda", "peso_inicial": 70},
        {"id": "foro", "nombre": "Foro de prueba", "tipo": "comunidad", "peso_inicial": 30},
    ]

    def base_con_datos():
        bd = conectar(":memory:")
        registrar_fuentes(bd, FUENTES)
        guardar_elemento(bd, "h1", "oficial", "Official booster reveal", "https://o.com/a", "en",
                         "noticia", "critica", "Resumen oficial", "h1")
        guardar_elemento(bd, "h2", "medio", "Anuncio del booster", "https://m.es/a", "es",
                         "noticia", "media", "", "h1")   # misma noticia que h1, en español
        guardar_elemento(bd, "h3", "foro", "Rumor sobre cartas", "https://f.com/a", "en",
                         "noticia", "baja", "", "h3")
        guardar_elemento(bd, "h4", "tienda", "Booster en preventa", "https://t.es/a", "es",
                         "preventa", "critica", "Precio 5.00 EUR", "h4")
        return bd

    # Construcción del texto
    bd = base_con_datos()
    partes, hay = partes_resumen(bd, elementos_desde(bd, "2000-01-01T00:00:00+00:00"))
    assert hay is True and len(partes) == 1
    texto = partes[0]
    assert texto.index("PREVENTAS") < texto.index("Crítica") < texto.index("Baja")
    assert "También en" in texto
    assert texto.count("Anuncio del booster") == 1            # aparece solo como mención extra
    assert texto.index("Official booster reveal") < texto.index("Anuncio del booster")
    assert "🟡 Media" not in texto                            # el grupo hereda la importancia más alta

    # Troceado
    trozos = _trocear(["x" * 1000] * 10)
    assert len(trozos) > 1 and all(len(t) <= LIMITE_PARTE for t in trozos)

    # Cuándo toca enviar (lunes 26/10/2026, Madrid está en UTC+1)
    cfg = {"resumenes": {"diario": True, "hora_diaria": "09:00", "semanal": True,
                         "dia_semanal": "lunes", "zona_horaria": "Europe/Madrid"}}
    bd2 = conectar(":memory:")
    utc = timezone.utc
    assert resumen_debido(bd2, cfg, datetime(2026, 10, 26, 7, 30, tzinfo=utc)) is None       # 08:30 en Madrid
    assert resumen_debido(bd2, cfg, datetime(2026, 10, 26, 8, 30, tzinfo=utc)) == "semanal"  # 09:30 en Madrid
    assert resumen_debido(bd2, cfg, datetime(2026, 10, 27, 8, 30, tzinfo=utc)) == "diario"   # martes

        # Resumen diario: envía, marca como enviado y no se repite
    martes = datetime(2026, 10, 27, 8, 30, tzinfo=utc)
    n = enviar_resumen(bd, cfg, "diario", ahora_utc=martes, enviar=falso)
    assert n == 3 and enviados[-1]["canal"] == "noticias"   # solo las 3 noticias; la preventa va por su canal
    pendientes = elementos_sin_enviar(bd)
    assert len(pendientes) == 1 and pendientes[0]["categoria"] == "preventa"
    assert leer_metadato(bd, "ultimo_resumen_diario") == "2026-10-27"
    assert resumen_debido(bd, cfg, martes) is None

    # Resumen semanal: marca también el diario del mismo día
    bd3 = base_con_datos()
    ahora_real = datetime.now(utc)
    enviar_resumen(bd3, cfg, "semanal", ahora_utc=ahora_real, enviar=falso)
    hoy = _local(cfg, ahora_real).date().isoformat()
    assert leer_metadato(bd3, "ultimo_resumen_semanal") == hoy
    assert leer_metadato(bd3, "ultimo_resumen_diario") == hoy

    # Sin noticias no se manda nada, pero se anota que ya tocó
    antes = len(enviados)
    bd_vacia = conectar(":memory:")
    assert enviar_resumen(bd_vacia, cfg, "diario", ahora_utc=martes, enviar=falso) == 0
    assert len(enviados) == antes
    assert leer_metadato(bd_vacia, "ultimo_resumen_diario") == "2026-10-27"

    # Recap tras estar apagado 5 horas: preventas destacadas y todo queda marcado como enviado
    bd4 = base_con_datos()
    ahora_real = datetime.now(utc)
    ultima = (ahora_real - timedelta(hours=5)).isoformat(timespec="seconds")
    antes = len(enviados)
    assert enviar_recap(bd4, ultima, ahora_utc=ahora_real, enviar=falso) == 4
    recap = enviados[antes]
    assert recap["canal"] == "noticias" and "5 h" in recap["titulo"] and recap["tipo"] == "preventa"
    assert elementos_sin_enviar(bd4) == []

    # Apagado poco tiempo: no hay recap
    bd5 = base_con_datos()
    corta = (ahora_real - timedelta(minutes=10)).isoformat(timespec="seconds")
    antes = len(enviados)
    assert enviar_recap(bd5, corta, ahora_utc=ahora_real, enviar=falso) == 0
    assert len(enviados) == antes

    # Primera ejecución de la historia: no hay recap
    assert enviar_recap(bd5, None, enviar=falso) == 0

    # Apagado mucho tiempo y sin novedades: aviso corto al canal de estado
    antes = len(enviados)
    assert enviar_recap(conectar(":memory:"), ultima, ahora_utc=ahora_real, enviar=falso) == 0
    assert enviados[antes]["canal"] == "estado"

    # Registrar la ejecución actual
    registrar_ejecucion(bd5)
    assert leer_metadato(bd5, "ultima_ejecucion") is not None

    # Formato de duraciones
    assert formatear_duracion(timedelta(minutes=45)) == "45 min"
    assert formatear_duracion(timedelta(hours=3, minutes=20)) == "3 h 20 min"
    assert formatear_duracion(timedelta(days=2, hours=4)) == "2 d 4 h"

    print("Resúmenes OK")