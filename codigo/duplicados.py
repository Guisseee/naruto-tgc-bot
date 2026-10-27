"""Duplicados: huellas de elementos y agrupación de la misma noticia entre fuentes e idiomas."""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit

from almacenamiento import elementos_desde
from clasificador import normalizar

# Parámetros de las direcciones que solo sirven para el seguimiento y no cambian la página
CLAVES_SEGUIMIENTO = {"fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "ref", "ref_src"}

# Palabras vacías (en varios idiomas) que no ayudan a reconocer una noticia
PALABRAS_VACIAS = {
    "para", "con", "por", "una", "uno", "los", "las", "del", "que", "como", "mas",
    "sus", "este", "esta", "desde", "sobre", "entre", "the", "and", "for", "with",
    "from", "has", "its", "will", "are", "this", "that", "into", "about", "after",
    "les", "des", "pour", "avec", "und", "der", "die", "das", "mit", "fur", "ein",
}

# Palabras del propio juego: aparecen en casi todas las noticias y no distinguen una de otra
PALABRAS_DEL_JUEGO = {
    "naruto", "card", "cards", "game", "tcg", "trading", "juego", "cartas", "carta",
    "jeu", "cartes", "spiel", "karten", "kartenspiel", "bandai",
}

IGNORADAS = PALABRAS_VACIAS | PALABRAS_DEL_JUEGO

# Para agrupar dos titulares hacen falta al menos 3 palabras en común,
# y que sean la mitad o más del titular más corto
MINIMO_COMUNES = 3
UMBRAL_SOLAPE = 0.5

# Qué fuente se destaca al agrupar (menor número = más fiable)
RANGO_TIPO = {"oficial": 0, "distribuidor": 1, "tienda": 2, "medio": 3, "comunidad": 4}


def _hash(texto):
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# ---------- Direcciones y huellas ----------

def normalizar_url(url):
    """Deja la dirección en una forma estable: sin esquema, sin www, sin # ni seguimiento."""
    partes = urlsplit((url or "").strip())
    host = partes.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    ruta = partes.path.rstrip("/")
    consulta = [
        (clave, valor)
        for clave, valor in parse_qsl(partes.query, keep_blank_values=True)
        if not clave.lower().startswith("utm_") and clave.lower() not in CLAVES_SEGUIMIENTO
    ]
    consulta.sort()
    texto = f"{host}{ruta}"
    if consulta:
        texto += "?" + urlencode(consulta)
    return texto


def huella_elemento(url, titulo):
    """Huella de una noticia: se basa en su dirección, y en el título si no hay dirección."""
    if url and url.strip():
        return _hash("url:" + normalizar_url(url))
    return _hash("titulo:" + " ".join(normalizar(titulo).split()))


def huella_tienda(url, estado, dia=None):
    """Huella de un aviso de tienda: dirección + estado + día (UTC).

    Si una ficha cambia a "preventa", vuelve a agotarse y al día siguiente vuelve a
    abrirse, el segundo aviso no se confunde con el primero.
    """
    dia = dia or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _hash(f"tienda:{normalizar_url(url)}|{estado}|{dia}")


# ---------- Agrupar la misma noticia entre fuentes e idiomas ----------

def palabras_significativas(titulo):
    """Palabras del titular que sirven para reconocerlo: nombres, cifras, años."""
    palabras = set()
    for palabra in re.findall(r"\w+", normalizar(titulo)):
        if palabra in IGNORADAS:
            continue
        if len(palabra) >= 3 or palabra.isdigit():
            palabras.add(palabra)
    return palabras


def misma_noticia(titulo_a, titulo_b):
    """True si los dos titulares parecen contar lo mismo (aunque estén en idiomas distintos)."""
    a = palabras_significativas(titulo_a)
    b = palabras_significativas(titulo_b)
    if not a or not b:
        return False
    comunes = len(a & b)
    if comunes < MINIMO_COMUNES:
        return False
    return comunes / min(len(a), len(b)) >= UMBRAL_SOLAPE


def asignar_grupo(conexion, titulo, huella, dias=7):
    """Devuelve el grupo al que pertenece una noticia nueva.

    Si en los últimos días ya hay una noticia parecida, devuelve su grupo.
    Si no, la noticia abre un grupo propio (el grupo es su propia huella).
    Solo para noticias: las preventas no se agrupan nunca.
    """
    desde = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat(timespec="seconds")
    for fila in elementos_desde(conexion, desde):
        if fila["categoria"] != "noticia":
            continue
        if misma_noticia(titulo, fila["titulo"]):
            return fila["grupo"]
    return huella


def miembros_grupo(conexion, grupo):
    """Todas las menciones de un grupo, de la más antigua a la más reciente."""
    return conexion.execute(
        """
        SELECT e.id, e.titulo, e.url, e.idioma, e.fuente_id, e.detectado_en,
               f.nombre AS fuente_nombre, f.tipo AS tipo, f.peso AS peso
        FROM elementos e
        LEFT JOIN fuentes f ON f.id = e.fuente_id
        WHERE e.grupo = ? AND e.falso = 0
        ORDER BY e.detectado_en, e.id
        """,
        (grupo,),
    ).fetchall()


def ordenar_por_fiabilidad(miembros):
    """Ordena las menciones de un grupo de más a menos fiable (la primera es la principal)."""
    return sorted(
        miembros,
        key=lambda m: (RANGO_TIPO.get(m["tipo"] or "comunidad", 9), -(m["peso"] or 0)),
    )


# ---------- Pruebas: python codigo/duplicados.py ----------

if __name__ == "__main__":
    from almacenamiento import conectar, guardar_elemento, registrar_fuentes

    # Direcciones: el seguimiento y los detalles menores no crean noticias distintas
    assert (
        normalizar_url("https://www.Ejemplo.com/noticia/?utm_source=x&id=5#seccion")
        == "ejemplo.com/noticia?id=5"
    )
    assert huella_elemento("https://www.ejemplo.com/n?utm_source=x", "A") == huella_elemento(
        "http://ejemplo.com/n/", "B"
    )
    assert huella_elemento("https://ejemplo.com/n", "A") != huella_elemento("https://ejemplo.com/m", "A")
    assert huella_elemento("", "Hola  Mundo") == huella_elemento(None, "hola mundo")

    # Tiendas: el estado y el día cambian la huella; el seguimiento no
    base = huella_tienda("https://t.com/p?utm_x=1", "preventa", dia="2026-10-26")
    assert base == huella_tienda("https://www.t.com/p", "preventa", dia="2026-10-26")
    assert base != huella_tienda("https://t.com/p", "disponible", dia="2026-10-26")
    assert base != huella_tienda("https://t.com/p", "preventa", dia="2026-10-27")

    # Agrupar titulares entre idiomas
    titulo_es = "Naruto Card Game revela el booster Enter Naruto Uzumaki para junio de 2027"
    titulo_en = "Naruto Card Game reveals Enter Naruto Uzumaki booster for June 2027"
    assert misma_noticia(titulo_es, titulo_en) is True
    assert misma_noticia(titulo_en, "Naruto Card Game starter deck Gaara revealed") is False
    assert misma_noticia(titulo_es, "Naruto Card Game llegará en junio de 2027") is False  # solo 2 en común
    assert misma_noticia("", titulo_en) is False

    # Con la base de datos: el segundo titular entra en el grupo del primero
    bd = conectar(":memory:")
    registrar_fuentes(bd, [
        {"id": "medio", "nombre": "Medio de prueba", "tipo": "medio", "peso_inicial": 60},
        {"id": "oficial", "nombre": "Web oficial", "tipo": "oficial", "peso_inicial": 95},
    ])

    h1 = huella_elemento("https://medio.es/a", titulo_es)
    g1 = asignar_grupo(bd, titulo_es, h1)
    assert g1 == h1, "La primera noticia debía abrir su propio grupo"
    guardar_elemento(bd, h1, "medio", titulo_es, "https://medio.es/a", "es",
                     "noticia", "media", grupo=g1)

    h2 = huella_elemento("https://oficial.com/b", titulo_en)
    g2 = asignar_grupo(bd, titulo_en, h2)
    assert g2 == h1, "El titular en inglés debía entrar en el grupo del español"
    guardar_elemento(bd, h2, "oficial", titulo_en, "https://oficial.com/b", "en",
                     "noticia", "alta", grupo=g2)

    miembros = miembros_grupo(bd, h1)
    assert len(miembros) == 2
    assert ordenar_por_fiabilidad(miembros)[0]["fuente_id"] == "oficial"

    otro = "Naruto Card Game starter deck Gaara revealed"
    h3 = huella_elemento("https://medio.es/c", otro)
    assert asignar_grupo(bd, otro, h3) == h3, "Una noticia distinta no debía agruparse"

    print("Duplicados OK")