"""Lectores: convierten páginas descargadas en datos que el bot entiende."""
import json
from dataclasses import dataclass
from urllib.parse import urljoin

import feedparser
from bs4 import BeautifulSoup


class ErrorLectura(Exception):
    """El texto descargado no tiene el formato esperado."""


@dataclass
class Elemento:
    """Una noticia o entrada detectada en una fuente."""
    titulo: str
    url: str
    resumen: str = ""
    fecha: str = ""
    fuente_id: str = ""
    idioma: str = ""


def limpiar_texto(texto):
    """Quita las etiquetas HTML y los espacios sobrantes."""
    if not texto:
        return ""
    sopa = BeautifulSoup(texto, "html.parser")
    return " ".join(sopa.get_text(" ").split())


# ---------- RSS ----------

def leer_rss(texto, fuente):
    """Lee un feed RSS o Atom y devuelve una lista de Elemento."""
    feed = feedparser.parse(texto)
    if not feed.entries and feed.bozo:
        raise ErrorLectura("El contenido no parece un feed RSS válido")

    elementos = []
    for entrada in feed.entries:
        titulo = limpiar_texto(entrada.get("title", ""))
        enlace = entrada.get("link", "")
        if not titulo or not enlace:
            continue
        elementos.append(Elemento(
            titulo=titulo,
            url=enlace,
            resumen=limpiar_texto(entrada.get("summary", ""))[:500],
            fecha=entrada.get("published", entrada.get("updated", "")),
            fuente_id=fuente["id"],
            idioma=fuente.get("idioma", ""),
        ))
    return elementos


# ---------- HTML de noticias ----------

def leer_html(texto, fuente):
    """Saca los titulares de una página de noticias.

    Recoge los enlaces cuyo texto tiene al menos 15 caracteres. Si una web da ruido,
    se puede afinar con un campo "selector" en fuentes.yml (un selector CSS que
    apunte a enlaces <a>, por ejemplo "article h2 a").
    """
    sopa = BeautifulSoup(texto, "html.parser")
    base = fuente["url"]
    selector = fuente.get("selector", "a[href]")

    vistos = set()
    elementos = []
    for enlace in sopa.select(selector):
        href = enlace.get("href")
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        titulo = " ".join(enlace.get_text(" ").split())
        if len(titulo) < 15:
            continue
        url = urljoin(base, href)
        if url in vistos:
            continue
        vistos.add(url)
        elementos.append(Elemento(
            titulo=titulo,
            url=url,
            fuente_id=fuente["id"],
            idioma=fuente.get("idioma", ""),
        ))
    return elementos


# ---------- Estado de una ficha de tienda ----------

# Valores de "availability" de schema.org -> nuestro estado
# (schema.org no tiene un valor para sorteos o invitaciones: eso se detecta por los botones)
ESTADOS_SCHEMA = {
    "preorder": "preventa",
    "presale": "preventa",
    "backorder": "preventa",   # reservable aunque aún no haya stock
    "instock": "disponible",
    "onlineonly": "disponible",
    "limitedavailability": "disponible",
    "instoreonly": "disponible",
    "outofstock": "agotado",
    "soldout": "agotado",
    "discontinued": "agotado",
}

# Palabras que aparecen en los botones de la ficha
PALABRAS_BOTON = {
    "sorteo": [
        "request an invitation", "request invitation", "invitation request",
        "solicitar invitación", "solicitar invitacion", "solicitar una invitación",
        "raffle", "lottery", "enter the draw", "sorteo", "participar en el sorteo",
        "cola virtual", "virtual queue", "join the queue", "waiting room", "抽選",
    ],
    "preventa": [
        "pre-order", "preorder", "pre order", "preventa", "reservar",
        "reserva ya", "précommande", "vorbestellen", "予約",
    ],
    "disponible": [
        "add to cart", "add to basket", "añadir al carrito", "añadir a la cesta",
        "comprar", "buy now", "in den warenkorb", "ajouter au panier",
    ],
    "agotado": [
        "sold out", "out of stock", "agotado", "sin stock", "no disponible",
        "currently unavailable", "notify me", "avísame", "ausverkauft",
        "épuisé", "rupture de stock",
    ],
}


def _recorrer(dato):
    """Recorre un JSON y devuelve todos los diccionarios que tengan "availability"."""
    if isinstance(dato, dict):
        if "availability" in dato:
            yield dato
        for valor in dato.values():
            yield from _recorrer(valor)
    elif isinstance(dato, list):
        for valor in dato:
            yield from _recorrer(valor)


def _contenido_meta(sopa, propiedad):
    meta = sopa.find("meta", attrs={"property": propiedad})
    return meta["content"].strip() if meta and meta.get("content") else None


def _estados_por_botones(sopa):
    """Devuelve el conjunto de estados que sugieren los textos de los botones de la página."""
    textos = [b.get_text(" ").strip().lower() for b in sopa.find_all("button")]
    for campo in sopa.find_all("input", attrs={"type": ["submit", "button"]}):
        textos.append((campo.get("value") or "").strip().lower())
    for enlace in sopa.find_all("a", attrs={"role": "button"}):
        textos.append(enlace.get_text(" ").strip().lower())

    detectados = set()
    for texto_boton in textos:
        for estado, palabras in PALABRAS_BOTON.items():
            if any(palabra in texto_boton for palabra in palabras):
                detectados.add(estado)
    return detectados


def leer_estado_tienda(texto, fuente=None):
    """Lee una ficha de producto y devuelve un diccionario con:
    estado ("sorteo", "preventa", "disponible", "agotado" o "desconocido"), precio,
    moneda, titulo y metodo ("json-ld" o "texto", cómo se llegó a esa conclusión).

    "sorteo" significa que se compra por sorteo, invitación o cola virtual: hay que
    apuntarse en la tienda. Solo se detecta por el texto de los botones.
    """
    sopa = BeautifulSoup(texto, "html.parser")

    titulo = _contenido_meta(sopa, "og:title") or ""
    if not titulo and sopa.title and sopa.title.string:
        titulo = sopa.title.string.strip()

    detectados = _estados_por_botones(sopa)

    # --- Método 1: datos estructurados JSON-LD (el más fiable) ---
    encontrados = []
    for script in sopa.find_all("script", attrs={"type": "application/ld+json"}):
        contenido = script.string or script.get_text()
        try:
            datos = json.loads(contenido)
        except (ValueError, TypeError):
            continue
        for oferta in _recorrer(datos):
            clave = str(oferta.get("availability", "")).rsplit("/", 1)[-1].lower()
            estado = ESTADOS_SCHEMA.get(clave)
            if estado:
                encontrados.append((estado, oferta))

    for preferido in ("preventa", "disponible", "agotado"):
        for estado, oferta in encontrados:
            if estado == preferido:
                # Una ficha "agotada" con botón de sorteo o invitación en realidad se puede pedir
                if estado == "agotado" and "sorteo" in detectados:
                    estado = "sorteo"
                precio = oferta.get("price")
                return {
                    "estado": estado,
                    "precio": str(precio) if precio is not None else None,
                    "moneda": oferta.get("priceCurrency"),
                    "titulo": titulo,
                    "metodo": "json-ld",
                }

    # --- Método 2: texto de los botones ---
    if "sorteo" in detectados:
        estado = "sorteo"   # prevalece sobre un "agotado" que suele ser solo el stock directo
    elif "agotado" in detectados and len(detectados) > 1:
        estado = "desconocido"   # botones contradictorios: no adivinamos
    elif "preventa" in detectados:
        estado = "preventa"
    elif "disponible" in detectados:
        estado = "disponible"
    elif "agotado" in detectados:
        estado = "agotado"
    else:
        estado = "desconocido"

    return {
        "estado": estado,
        "precio": _contenido_meta(sopa, "product:price:amount"),
        "moneda": _contenido_meta(sopa, "product:price:currency"),
        "titulo": titulo,
        "metodo": "texto",
    }


# ---------- Pruebas: python codigo/lectores.py ----------

if __name__ == "__main__":
    # RSS
    rss = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Prueba</title>'
        "<item><title>Naruto Card Game: nueva expansión</title>"
        "<link>https://ejemplo.com/a</link>"
        "<description>&lt;p&gt;Resumen &lt;b&gt;uno&lt;/b&gt;&lt;/p&gt;</description>"
        "<pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"
    )
    elementos = leer_rss(rss, {"id": "rss_prueba", "idioma": "es"})
    assert len(elementos) == 1
    assert elementos[0].titulo == "Naruto Card Game: nueva expansión"
    assert elementos[0].resumen == "Resumen uno"
    assert elementos[0].fuente_id == "rss_prueba"

    # HTML de noticias
    html = (
        '<a href="/news/01">Nuevas cartas reveladas para el juego</a>'
        '<a href="/news/01">Nuevas cartas reveladas para el juego</a>'
        '<a href="#">Inicio</a>'
    )
    fuente_html = {"id": "web_prueba", "url": "https://ejemplo.com/noticias/", "idioma": "es"}
    elementos = leer_html(html, fuente_html)
    assert len(elementos) == 1
    assert elementos[0].url == "https://ejemplo.com/news/01"

    # Tienda: datos estructurados
    ficha = (
        '<html><head><title>Booster Box</title>'
        '<script type="application/ld+json">{"@type":"Product","name":"Booster Box",'
        '"offers":{"@type":"Offer","price":"89.90","priceCurrency":"EUR",'
        '"availability":"https://schema.org/PreOrder"}}</script></head></html>'
    )
    r = leer_estado_tienda(ficha)
    assert r["estado"] == "preventa" and r["precio"] == "89.90" and r["moneda"] == "EUR"
    assert r["metodo"] == "json-ld"

    # Tienda: texto de botones
    assert leer_estado_tienda("<button>Sold out</button>")["estado"] == "agotado"
    assert leer_estado_tienda("<button>Pre-order now</button>")["estado"] == "preventa"
    assert leer_estado_tienda("<button>Add to cart</button>")["estado"] == "disponible"
    # Botones contradictorios: no se adivina
    mezcla = "<button>Add to cart</button><button>Sold out</button>"
    assert leer_estado_tienda(mezcla)["estado"] == "desconocido"
    # Sin ninguna pista
    assert leer_estado_tienda("<p>Hola</p>")["estado"] == "desconocido"

    # Sorteo, invitación o cola
    assert leer_estado_tienda("<button>Request an invitation</button>")["estado"] == "sorteo"
    assert leer_estado_tienda("<button>Solicitar invitación</button>")["estado"] == "sorteo"
    assert leer_estado_tienda('<a role="button">Join the queue</a>')["estado"] == "sorteo"
    # El sorteo prevalece sobre un "agotado" que convive en la página
    assert leer_estado_tienda("<button>Sold out</button><button>Enter the raffle</button>")["estado"] == "sorteo"
    # Datos estructurados "agotado" + botón de invitación = sorteo, conservando el precio
    ficha_sorteo = (
        '<html><head><title>Booster Box</title>'
        '<script type="application/ld+json">{"@type":"Product","name":"Booster Box",'
        '"offers":{"@type":"Offer","price":"89.90","priceCurrency":"EUR",'
        '"availability":"https://schema.org/OutOfStock"}}</script></head>'
        "<body><button>Request an invitation</button></body></html>"
    )
    r = leer_estado_tienda(ficha_sorteo)
    assert r["estado"] == "sorteo" and r["precio"] == "89.90" and r["metodo"] == "json-ld"

    print("Lectores OK")