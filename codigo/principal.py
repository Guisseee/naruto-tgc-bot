"""Punto de entrada del bot: revisa las fuentes, avisa de preventas y envía resúmenes."""
import argparse
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import yaml

from almacenamiento import (
    conectar,
    elementos_sin_enviar,
    existe_huella,
    guardar_elemento,
    guardar_estado_tienda,
    guardar_metadato,
    leer_metadato,
    marcar_enviado,
    marcar_falso,
    obtener_estado_tienda,
    registrar_fuentes,
    registrar_resultado_fuente,
)
from avisos import enviar_aviso
from clasificador import ETIQUETAS, clasificar_elemento, clasificar_tienda
from descargador import BloqueadoPorRobots, Descargador, ErrorDescarga
from duplicados import asignar_grupo, huella_elemento, huella_tienda
from fiabilidad import debe_avisar_fallo, registrar_nuevo_elemento
from lectores import ErrorLectura, leer_estado_tienda, leer_html, leer_rss
from resumenes import enviar_recap, enviar_resumen, registrar_ejecucion, resumen_debido

RAIZ = Path(__file__).resolve().parent.parent

# Máximo de noticias críticas que se avisan de golpe por fuente y pasada.
# Protege de una avalancha si una web cambia de formato; el resto va al resumen.
MAX_URGENTES_POR_FUENTE = 5

TITULOS_ESTADO = {
    "preventa": "🚨 PREVENTA detectada",
    "disponible": "🚨 DISPONIBLE para comprar",
    "sorteo": "🎟️ SORTEO / INVITACIÓN detectado",
}


# ---------- Utilidades ----------

def escribir_log(texto):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {texto}", flush=True)


def cargar_env(ruta=None):
    """Lee el archivo .env (si existe) y carga sus variables.

    En GitHub Actions no hay .env: allí las variables llegan como Secrets.
    """
    archivo = Path(ruta) if ruta else RAIZ / ".env"
    if not archivo.exists():
        return
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip())


def cargar_yaml(ruta):
    if not ruta.exists():
        raise SystemExit(f"No encuentro el archivo {ruta}")
    datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    if not isinstance(datos, dict):
        raise SystemExit(f"{ruta.name} está vacío o mal escrito (¿falta guardarlo?)")
    return datos


def fuentes_activas(datos):
    """Devuelve las fuentes con activa: true y una dirección válida."""
    activas = []
    for f in datos.get("fuentes") or []:
        if not f.get("activa", False):
            continue
        faltan = [c for c in ("id", "nombre", "tipo", "url") if not f.get(c)]
        if faltan:
            escribir_log(f"Fuente ignorada ({f.get('id', '?')}): faltan los campos {faltan}")
            continue
        if not str(f["url"]).startswith(("http://", "https://")):
            escribir_log(f"Fuente ignorada ({f['id']}): su url no es una dirección válida")
            continue
        activas.append(f)
    return activas


def hora_local(config, momento):
    zona = ZoneInfo(config.get("resumenes", {}).get("zona_horaria", "Europe/Madrid"))
    return momento.astimezone(zona).strftime("%d/%m/%Y %H:%M")


def mencion_preventa():
    """Menciona a tu usuario si has puesto DISCORD_USER_ID en el .env; si no, a todos (@everyone)."""
    usuario = os.environ.get("DISCORD_USER_ID", "").strip()
    return f"<@{usuario}>" if usuario.isdigit() else "@everyone"


@dataclass
class Contexto:
    bd: object
    config: dict
    descargador: object
    enviar: Callable = enviar_aviso   # puede lanzar RuntimeError si Discord falla
    ahora: Callable = lambda: datetime.now(timezone.utc)


def enviar_seguro(ctx, **datos):
    """Envía un aviso. Devuelve True si salió bien y False si falló (sin romper el bot)."""
    try:
        ctx.enviar(**datos)
        return True
    except RuntimeError as error:
        escribir_log(f"No se pudo enviar a Discord: {error}")
        return False


# ---------- Textos de los avisos ----------

def texto_envio(fuente):
    """Información sobre envíos a España. No se inventa nada: se basa en tu configuración."""
    pais = str(fuente.get("pais", "")).upper()
    if pais == "ES":
        return None
    envia = fuente.get("envia_a_espana")
    if envia is True:
        base = "Consta que envía a España/UE (según tu configuración)."
    elif envia is False:
        base = "Consta que NO envía a España (según tu configuración)."
    else:
        base = "No consta si envía a España: compruébalo en la tienda."
    return base + " Gastos de envío, aduanas e impuestos: revísalos en la tienda, el bot no los conoce."


def construir_aviso_tienda(ctx, fuente, aviso):
    """Devuelve (título, descripción) del aviso de una ficha de tienda."""
    producto = aviso["titulo"] or fuente["nombre"]
    titulo = f"{TITULOS_ESTADO.get(aviso['estado'], '🚨 Cambio en tienda')}: {producto}"

    lineas = [
        f"**Tienda:** {fuente['nombre']} ({fuente.get('pais', '?')})",
        f"**Producto:** {producto}",
    ]
    if aviso.get("precio"):
        lineas.append(f"**Precio:** {aviso['precio']} {aviso.get('moneda') or ''}".strip())
    lineas.append(f"**Idioma de la página:** {str(fuente.get('idioma', '?')).upper()}")
    if aviso["acceso"] == "sorteo":
        lineas.append(
            "**Cómo se accede:** por sorteo o invitación. Hay que apuntarse en la tienda; "
            "el bot no lo hace por ti."
        )
    envio = texto_envio(fuente)
    if envio:
        lineas.append(f"**Envío:** {envio}")
    if aviso.get("primera_vez"):
        lineas.append("Primera lectura de esta ficha: ya estaba así cuando empecé a vigilarla.")
    lineas.append(aviso["etiqueta"])
    lineas.append(f"**Enlace:** {fuente['url']}")
    lineas.append(f"**Detectado:** {hora_local(ctx.config, ctx.ahora())} (hora de Madrid)")
    return titulo, "\n".join(lineas)


def construir_aviso_noticia(ctx, fuente, elemento, clas):
    """Devuelve (título, descripción) del aviso de una noticia crítica o de una preventa anunciada."""
    preventa = clas["categoria"] == "preventa"
    sorteo = clas["acceso"] == "sorteo"
    if preventa:
        prefijo = "🎟️ Posible SORTEO / INVITACIÓN" if sorteo else "🚨 PREVENTA anunciada"
    else:
        prefijo = "🔴 Noticia crítica"
    titulo = f"{prefijo}: {elemento.titulo}"

    lineas = []
    if clas["es_rumor"]:
        lineas.append("⚠️ **RUMOR sin confirmar**")
    lineas.append(
        f"{clas['etiqueta']} · {fuente['nombre']} ({fuente.get('pais', '?')}) · "
        f"idioma {str(elemento.idioma or '?').upper()}"
    )
    if elemento.resumen and elemento.resumen != elemento.titulo:
        lineas.append(" ".join(elemento.resumen.split())[:300])
    if preventa:
        lineas.append(
            "Detectado en una noticia, no en la ficha de una tienda: "
            "comprueba en la tienda si ya se puede reservar."
        )
        if sorteo:
            lineas.append("Parece que se compra por sorteo o invitación: hay que apuntarse en la tienda.")
        envio = texto_envio(fuente)
        if envio:
            lineas.append(f"**Envío:** {envio}")
    lineas.append(f"**Enlace:** {elemento.url}")
    lineas.append(f"**Detectado:** {hora_local(ctx.config, ctx.ahora())} (hora de Madrid)")
    return titulo, "\n".join(lineas)


# ---------- Procesar una fuente ----------

def procesar_noticias(ctx, fuente, elementos):
    """Clasifica las noticias de una fuente, las guarda y avisa de las urgentes."""
    bd = ctx.bd
    clave = f"sembrada:{fuente['id']}"
    sembrada = leer_metadato(bd, clave) == "1"
    urgentes = 0

    for e in elementos:
        clas = clasificar_elemento(e, fuente, ctx.config)
        if clas is None:
            continue
        huella = huella_elemento(e.url, e.titulo)
        if existe_huella(bd, huella):
            continue

        if not sembrada:
            # Primera lectura de esta fuente: solo se toma nota de lo que ya había.
            # Se guardan ocultas (no salen en resúmenes ni en el recap) y sin avisar.
            nuevo = guardar_elemento(
                bd, huella, fuente["id"], e.titulo, e.url, e.idioma,
                clas["categoria"], clas["importancia"], e.resumen, huella,
            )
            if nuevo:
                marcar_enviado(bd, nuevo)
                marcar_falso(bd, nuevo)
            continue

        es_preventa = clas["categoria"] == "preventa"
        urgente = es_preventa or clas["importancia"] == "critica"
        grupo = huella if es_preventa else asignar_grupo(bd, e.titulo, huella)

        titulo_aviso, descripcion = construir_aviso_noticia(ctx, fuente, e, clas)
        nuevo = guardar_elemento(
            bd, huella, fuente["id"], e.titulo, e.url, e.idioma,
            clas["categoria"], clas["importancia"],
            descripcion if es_preventa else e.resumen, grupo,
        )
        if nuevo is None:
            continue
        registrar_nuevo_elemento(bd, fuente["id"], abre_grupo=(grupo == huella))

        # Si la misma noticia ya se avisó desde otra fuente o idioma, no se repite
        if grupo != huella and bd.execute(
            "SELECT 1 FROM elementos WHERE grupo = ? AND enviado = 1 AND id != ?", (grupo, nuevo)
        ).fetchone():
            marcar_enviado(bd, nuevo)
            continue

        if not urgente:
            continue  # se queda pendiente para el resumen diario

        if es_preventa:
            enviado = enviar_seguro(
                ctx, canal="preventas", titulo=titulo_aviso, descripcion=descripcion,
                tipo="preventa", mencion=mencion_preventa(),
            )
        else:
            if urgentes >= MAX_URGENTES_POR_FUENTE:
                continue
            urgentes += 1
            enviado = enviar_seguro(
                ctx, canal="noticias", titulo=titulo_aviso, descripcion=descripcion, tipo="noticia",
            )
        if enviado:
            marcar_enviado(bd, nuevo)

    # Solo se da por "sembrada" si la lectura devolvió algo (una página vacía puede ser un fallo)
    if elementos and not sembrada:
        guardar_metadato(bd, clave, "1")


def procesar_tienda(ctx, fuente, texto):
    """Lee la ficha de una tienda y avisa si cambió a preventa, disponible o sorteo."""
    bd = ctx.bd
    url = fuente["url"]
    resultado = leer_estado_tienda(texto, fuente)
    previo = obtener_estado_tienda(bd, url)
    estado_anterior = previo["estado"] if previo else None

    aviso = clasificar_tienda(resultado, fuente, ctx.config, estado_anterior)
    if aviso:
        dia = ctx.ahora().strftime("%Y-%m-%d")
        huella = huella_tienda(url, resultado["estado"], dia)
        if not existe_huella(bd, huella):
            titulo_aviso, descripcion = construir_aviso_tienda(ctx, fuente, aviso)
            nuevo = guardar_elemento(
                bd, huella, fuente["id"], aviso["titulo"] or fuente["nombre"], url,
                fuente.get("idioma", ""), "preventa", "critica", descripcion, huella,
            )
            if nuevo:
                registrar_nuevo_elemento(bd, fuente["id"], abre_grupo=True)
                if enviar_seguro(
                    ctx, canal="preventas", titulo=titulo_aviso, descripcion=descripcion,
                    tipo="preventa", mencion=mencion_preventa(),
                ):
                    marcar_enviado(bd, nuevo)

    # Una lectura dudosa ("desconocido") nunca pisa un estado que ya conocíamos
    if resultado["estado"] != "desconocido":
        guardar_estado_tienda(bd, url, resultado["estado"], resultado.get("precio"))


def procesar_fuente(ctx, fuente):
    texto = ctx.descargador.descargar(fuente["url"])
    metodo = fuente.get("metodo", "html")
    if metodo == "tienda":
        procesar_tienda(ctx, fuente, texto)
    elif metodo == "rss":
        procesar_noticias(ctx, fuente, leer_rss(texto, fuente))
    elif metodo == "html":
        procesar_noticias(ctx, fuente, leer_html(texto, fuente))
    else:
        raise ErrorLectura(f"Método desconocido: «{metodo}». Usa rss, html o tienda")


# ---------- Una pasada completa ----------

def intervalo_minutos(fuente, config):
    """Cada cuántos minutos se revisa una fuente (se puede forzar con "minutos:" en fuentes.yml)."""
    if fuente.get("minutos"):
        return float(fuente["minutos"])
    revision = config.get("revision", {})
    if fuente.get("metodo") == "tienda" or fuente.get("tipo") == "tienda":
        return float(revision.get("minutos_tiendas", 10))
    return float(revision.get("minutos_noticias", 30))


def toca_revisar(ctx, fuente, forzar=False):
    if forzar:
        return True
    ultima = leer_metadato(ctx.bd, f"ultima_revision:{fuente['id']}")
    if not ultima:
        return True
    try:
        desde = datetime.fromisoformat(ultima)
    except ValueError:
        return True
    # Un margen de 90 s absorbe los retrasos de GitHub Actions
    espera = timedelta(minutes=intervalo_minutos(fuente, ctx.config)) - timedelta(seconds=90)
    return ctx.ahora() - desde >= espera


def anotar_fallo(ctx, fuente, motivo):
    fallos = registrar_resultado_fuente(ctx.bd, fuente["id"], ok=False)
    escribir_log(f"Fallo en {fuente['id']} ({fallos} seguidos): {motivo}")
    if debe_avisar_fallo(fallos):
        enviar_seguro(
            ctx, canal="estado", titulo="⚠️ Fuente con problemas",
            descripcion=(
                f"**{fuente['nombre']}** (`{fuente['id']}`) lleva {fallos} fallos seguidos.\n"
                f"Último error: {motivo}\n"
                "Si ya no te interesa, ponla en `activa: false` en fuentes.yml."
            ),
            tipo="estado",
        )


def reenviar_pendientes(ctx):
    """Reintenta las preventas que no se pudieron enviar (Discord caído, por ejemplo)."""
    for fila in elementos_sin_enviar(ctx.bd, "preventa"):
        if enviar_seguro(
            ctx, canal="preventas", titulo=f"🚨 (reenvío) {fila['titulo']}",
            descripcion=fila["resumen"] or fila["url"] or fila["titulo"],
            tipo="preventa", mencion=mencion_preventa(),
        ):
            marcar_enviado(ctx.bd, fila["id"])


def ejecutar_pasada(ctx, fuentes, forzar=False):
    """Revisa las fuentes que toquen. Devuelve cuántas se revisaron y cuántas fallaron."""
    reenviar_pendientes(ctx)
    revisadas = fallos = 0
    for fuente in fuentes:
        if not toca_revisar(ctx, fuente, forzar):
            continue
        revisadas += 1
        try:
            procesar_fuente(ctx, fuente)
            registrar_resultado_fuente(ctx.bd, fuente["id"], ok=True)
        except BloqueadoPorRobots as error:
            fallos += 1
            anotar_fallo(ctx, fuente, f"robots.txt no lo permite ({error})")
        except (ErrorDescarga, ErrorLectura) as error:
            fallos += 1
            anotar_fallo(ctx, fuente, str(error))
        except Exception as error:  # cualquier fallo inesperado no debe parar el bot
            fallos += 1
            anotar_fallo(ctx, fuente, f"{type(error).__name__}: {error}")
        guardar_metadato(ctx.bd, f"ultima_revision:{fuente['id']}", ctx.ahora().isoformat(timespec="seconds"))
    return {"revisadas": revisadas, "fallos": fallos}


def ciclo_resumenes(ctx):
    """Envía el resumen diario o semanal si toca."""
    try:
        tipo = resumen_debido(ctx.bd, ctx.config, ctx.ahora())
        if tipo:
            n = enviar_resumen(ctx.bd, ctx.config, tipo, ahora_utc=ctx.ahora(), enviar=ctx.enviar)
            escribir_log(f"Resumen {tipo} enviado ({n} noticias)")
    except RuntimeError as error:
        escribir_log(f"No se pudo enviar el resumen: {error}")


def arranque(ctx, fuentes, forzar=False):
    """Pasada inicial: revisa, envía el recap de lo que te perdiste y los resúmenes pendientes."""
    registrar_fuentes(ctx.bd, fuentes)
    ultima = leer_metadato(ctx.bd, "ultima_ejecucion")
    resultado = ejecutar_pasada(ctx, fuentes, forzar)
    escribir_log(f"Revisadas {resultado['revisadas']} fuentes ({resultado['fallos']} con fallo)")
    try:
        enviar_recap(ctx.bd, ultima, ahora_utc=ctx.ahora(), enviar=ctx.enviar)
    except RuntimeError as error:
        escribir_log(f"No se pudo enviar el recap: {error}")
    ciclo_resumenes(ctx)
    registrar_ejecucion(ctx.bd)


def modo_daemon(ctx, fuentes, forzar=False):
    arranque(ctx, fuentes, forzar)
    escribir_log("Bot en marcha. Pulsa Ctrl+C para detenerlo.")
    try:
        while True:
            time.sleep(30)
            try:
                ejecutar_pasada(ctx, fuentes)
                ciclo_resumenes(ctx)
                registrar_ejecucion(ctx.bd)
            except Exception as error:
                escribir_log(f"Error inesperado (el bot sigue): {type(error).__name__}: {error}")
    except KeyboardInterrupt:
        escribir_log("Bot detenido.")


# ---------- Autoprueba (sin internet ni Discord) ----------

def autoprueba():
    """Prueba todo el recorrido con fuentes y un Discord falsos. No usa internet."""

    class DescargadorFalso:
        def __init__(self, paginas):
            self.paginas = paginas

        def descargar(self, url):
            pagina = self.paginas[url]
            if isinstance(pagina, Exception):
                raise pagina
            return pagina

    enviados = []
    estado = {"fallar": False}

    def falso(canal, titulo, descripcion, tipo="estado", mencion=None):
        if estado["fallar"] and canal == "preventas":
            raise RuntimeError("fallo simulado")
        enviados.append({"canal": canal, "titulo": titulo, "texto": descripcion, "mencion": mencion})

    def por_canal(canal):
        return [m for m in enviados if m["canal"] == canal]

    def rss(*titulos):
        items = "".join(
            f"<item><title>{t}</title><link>https://oficial.test/{i}</link>"
            f"<description>{t}</description></item>"
            for i, t in enumerate(titulos)
        )
        return f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{items}</channel></rss>'

    def ficha(boton):
        return (
            '<html><head><meta property="og:title" content="Naruto Card Game Booster Box">'
            f"</head><body><button>{boton}</button></body></html>"
        )

    cfg = {
        "palabras_clave": {"en": ["naruto card game"]},
        "exclusiones": ["mythos"],
        "preventa": {"en": ["pre-order"]},
        "sorteo": {"en": ["request an invitation"]},
        "criticas": ["release date"],
        "revision": {"minutos_tiendas": 10, "minutos_noticias": 30},
        "resumenes": {"zona_horaria": "Europe/Madrid"},
    }
    fuentes = [
        {"id": "oficial", "nombre": "Web oficial", "tipo": "oficial", "metodo": "rss",
         "url": "https://oficial.test/rss", "pais": "JP", "idioma": "en", "peso_inicial": 95},
        {"id": "tienda", "nombre": "Tienda de prueba", "tipo": "tienda", "metodo": "tienda",
         "url": "https://tienda.test/ficha", "pais": "ES", "idioma": "es", "peso_inicial": 70},
        {"id": "rota", "nombre": "Web rota", "tipo": "medio", "metodo": "html",
         "url": "https://rota.test/", "pais": "US", "idioma": "en", "peso_inicial": 60},
    ]
    paginas = {
        "https://oficial.test/rss": rss("Naruto Card Game new card art",
                                        "Naruto Card Game release date announced"),
        "https://tienda.test/ficha": ficha("Sold out"),
        "https://rota.test/": ErrorDescarga("simulado"),
    }

    bd = conectar(":memory:")
    registrar_fuentes(bd, fuentes)
    ctx = Contexto(bd, cfg, DescargadorFalso(paginas), enviar=falso)

    def pasada():
        return ejecutar_pasada(ctx, fuentes, forzar=True)

    # Pasada 1: primera lectura. Solo se toma nota, no se avisa de nada.
    pasada()
    assert enviados == [], "La primera lectura no debía avisar de nada"
    assert elementos_sin_enviar(bd) == []

    # Pasada 2: aparece una preventa en la web oficial y la tienda pasa a preventa
    paginas["https://oficial.test/rss"] = rss(
        "Naruto Card Game new card art",
        "Naruto Card Game release date announced",
        "Naruto Card Game pre-order opens",
        "Naruto Mythos TCG news",
        "Naruto Card Game starter deck Gaara revealed",
    )
    paginas["https://tienda.test/ficha"] = ficha("Pre-order now")
    pasada()
    assert len(por_canal("preventas")) == 2, "Debían salir 2 avisos de preventa"
    assert all(m["mencion"] for m in por_canal("preventas")), "Los avisos de preventa deben mencionarte"
    assert len(elementos_sin_enviar(bd, "noticia")) == 1, "El mazo de Gaara debía quedar para el resumen"

    # Pasada 3: nada cambia, no debe repetir avisos
    pasada()
    assert len(por_canal("preventas")) == 2, "No debía repetir avisos"

    # Pasada 4: la tienda pasa a sorteo o invitación
    paginas["https://tienda.test/ficha"] = ficha("Request an invitation")
    pasada()
    assert len(por_canal("preventas")) == 3
    assert "SORTEO" in por_canal("preventas")[-1]["titulo"]

    # Pasada 5: la fuente rota llega a 5 fallos seguidos y se avisa una vez al canal de estado
    pasada()
    assert len(por_canal("estado")) == 1 and "5 fallos" in por_canal("estado")[0]["texto"]

    # Pasada 6: Discord falla al enviar la preventa; queda pendiente
    estado["fallar"] = True
    paginas["https://tienda.test/ficha"] = ficha("Add to cart")
    pasada()
    assert len(por_canal("preventas")) == 3
    assert len(elementos_sin_enviar(bd, "preventa")) == 1

    # Pasada 7: Discord vuelve y el aviso pendiente se reenvía
    estado["fallar"] = False
    pasada()
    assert len(por_canal("preventas")) == 4
    assert por_canal("preventas")[-1]["titulo"].startswith("🚨 (reenvío)")
    assert elementos_sin_enviar(bd, "preventa") == []

    # Sin forzar, las fuentes recién revisadas no se vuelven a pedir
    assert ejecutar_pasada(ctx, fuentes)["revisadas"] == 0

    # Recap tras 5 horas apagado: solo lo que NO se avisó ya (el mazo de Gaara)
    ahora = datetime.now(timezone.utc)
    ultima = (ahora - timedelta(hours=5)).isoformat(timespec="seconds")
    n = enviar_recap(bd, ultima, ahora_utc=ahora, enviar=falso)
    assert n == 1, (
        f"El recap debía incluir 1 elemento y incluyó {n}. "
        "Revisa que hiciste los dos cambios del paso 47 en resumenes.py"
    )

    print("Autoprueba OK")


# ---------- Línea de comandos ----------

def main():
    parser = argparse.ArgumentParser(description="Bot de avisos del NARUTO CARD GAME")
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--prueba", action="store_true", help="Envía un aviso de prueba a #estado-bot")
    modo.add_argument("--autoprueba", action="store_true", help="Prueba todo el recorrido sin internet ni Discord")
    modo.add_argument("--una-vez", action="store_true", help="Hace una pasada y termina (GitHub Actions)")
    modo.add_argument("--daemon", action="store_true", help="Se queda funcionando en local")
    parser.add_argument("--forzar", action="store_true", help="Revisa todas las fuentes sin esperar su intervalo")
    parser.add_argument("--bd", help="Ruta de la base de datos (por defecto datos/bot.db)")
    args = parser.parse_args()

    cargar_env()

    if args.autoprueba:
        autoprueba()
        return

    if args.prueba:
        enviar_aviso(
            canal="estado",
            titulo="✅ Prueba de conexión",
            descripcion="Si ves este mensaje, el bot ya puede escribir en Discord.",
            tipo="estado",
        )
        print("Aviso de prueba enviado. Mira el canal #estado-bot.")
        return

    if not (args.una_vez or args.daemon):
        parser.print_help()
        return

    config = cargar_yaml(RAIZ / "configuracion" / "configuracion.yml")
    datos = cargar_yaml(RAIZ / "configuracion" / "fuentes.yml")
    fuentes = fuentes_activas(datos)
    if not fuentes:
        escribir_log("No hay fuentes activas: edita fuentes.yml y pon activa: true en alguna.")

    ruta_bd = args.bd or os.environ.get("RUTA_BD") or str(RAIZ / "datos" / "bot.db")
    bd = conectar(ruta_bd)
    pausa = config.get("revision", {}).get("pausa_segundos_por_dominio", 5)
    descargador = Descargador(pausa_segundos=pausa)
    ctx = Contexto(bd, config, descargador)

    try:
        if args.una_vez:
            arranque(ctx, fuentes, args.forzar)
        else:
            modo_daemon(ctx, fuentes, args.forzar)
    finally:
        descargador.cerrar()
        bd.close()


if __name__ == "__main__":
    main()