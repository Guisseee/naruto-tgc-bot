"""Clasificador: decide si un elemento trata del juego, si es preventa y su importancia."""
import re
import unicodedata

# Etiquetas de fiabilidad que se muestran en los avisos
ETIQUETAS = {
    "oficial": "🟢 Oficial",
    "distribuidor": "🟢 Distribuidor oficial",
    "tienda": "🔵 Tienda con ficha de producto",
    "medio": "🟡 Medio especializado",
    "comunidad": "🔴 Rumor / comunidad (sin confirmar)",
}

# Tipos de fuente que pueden generar un aviso de preventa
TIPOS_CON_PREVENTA = ("oficial", "distribuidor", "tienda")

# Importancia base de una noticia normal, según el tipo de fuente
IMPORTANCIA_BASE = {
    "oficial": "alta",
    "distribuidor": "alta",
    "tienda": "media",
    "medio": "media",
    "comunidad": "baja",
}

# Importancia máxima a la que puede llegar una noticia crítica, según el tipo de fuente
IMPORTANCIA_MAXIMA = {
    "oficial": "critica",
    "distribuidor": "critica",
    "tienda": "critica",
    "medio": "alta",
    "comunidad": "media",
}


def normalizar(texto):
    """Pasa a minúsculas y quita las tildes, para comparar sin fijarse en ellas."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.lower()


def _contiene(texto_normalizado, termino):
    """True si el término aparece en el texto (ya normalizado).

    Los términos en alfabeto latino deben empezar una palabra: "reserva" encaja
    con "reservar", pero "preorder" no encaja dentro de otra palabra distinta.
    """
    t = normalizar(termino).strip()
    if not t:
        return False
    if t[0].isascii() and t[0].isalnum():
        return re.search(r"(?<!\w)" + re.escape(t), texto_normalizado) is not None
    return t in texto_normalizado  # japonés y otros alfabetos: búsqueda directa


def _aplanar(valor):
    """Convierte {"es": [...], "en": [...]} (o una lista) en una única lista."""
    if isinstance(valor, dict):
        return [t for lista in valor.values() for t in (lista or [])]
    return list(valor or [])


def _primera_coincidencia(texto, terminos):
    normalizado = normalizar(texto)
    for termino in terminos:
        if _contiene(normalizado, termino):
            return termino
    return None


# ---------- Preguntas sueltas ----------

def es_del_juego(texto, config):
    """Devuelve (True/False, motivo). Las exclusiones tienen prioridad."""
    excluido = _primera_coincidencia(texto, _aplanar(config.get("exclusiones")))
    if excluido:
        return False, f"excluido por «{excluido}»"
    clave = _primera_coincidencia(texto, _aplanar(config.get("palabras_clave")))
    if clave:
        return True, f"coincide con «{clave}»"
    return False, "no menciona el juego"


def menciona_preventa(texto, config):
    return _primera_coincidencia(texto, _aplanar(config.get("preventa"))) is not None


def es_critica(texto, config):
    return _primera_coincidencia(texto, _aplanar(config.get("criticas"))) is not None


# ---------- Noticias (RSS y webs) ----------

def clasificar_elemento(elemento, fuente, config):
    """Clasifica una noticia. Devuelve un diccionario, o None si no es del juego.

    "elemento" debe tener los atributos titulo y resumen (como el Elemento de lectores.py).
    """
    texto = f"{elemento.titulo} {elemento.resumen}"
    relevante, motivo = es_del_juego(texto, config)
    if not relevante:
        return None

    tipo = fuente.get("tipo", "comunidad")
    preventa = menciona_preventa(texto, config)
    critica = es_critica(texto, config)

    if preventa and tipo in TIPOS_CON_PREVENTA:
        categoria, importancia = "preventa", "critica"
    else:
        categoria = "noticia"
        if preventa or critica:
            importancia = IMPORTANCIA_MAXIMA.get(tipo, "media")
        else:
            importancia = IMPORTANCIA_BASE.get(tipo, "baja")

    return {
        "categoria": categoria,
        "importancia": importancia,
        "etiqueta": ETIQUETAS.get(tipo, ETIQUETAS["comunidad"]),
        "es_rumor": tipo == "comunidad",
        "menciona_preventa": preventa,
        "motivo": motivo,
    }


# ---------- Tiendas ----------

def es_novedad_tienda(estado_anterior, estado_nuevo):
    """True si el estado de una ficha ha cambiado a algo que merece aviso.

    estado_anterior es el último estado CONOCIDO (None si es la primera vez).
    Quien llame a esta función no debe guardar "desconocido" sobre un estado
    conocido, para no provocar avisos repetidos por lecturas fallidas.
    """
    return estado_nuevo in ("preventa", "disponible") and estado_anterior in (None, "agotado")


def clasificar_tienda(resultado, fuente, config, estado_anterior):
    """Decide si la lectura de una ficha de tienda merece un aviso de preventa.

    "resultado" es el diccionario que devuelve leer_estado_tienda (lectores.py).
    Devuelve un diccionario con los datos del aviso, o None si no hay que avisar.
    """
    if resultado["estado"] == "desconocido":
        return None

    # Protección contra falsos positivos: el título debe hablar del juego,
    # salvo que hayas marcado la ficha como verificada en fuentes.yml
    if not fuente.get("ficha_verificada", False):
        relevante, _ = es_del_juego(resultado.get("titulo", ""), config)
        if not relevante:
            return None

    if not es_novedad_tienda(estado_anterior, resultado["estado"]):
        return None

    return {
        "categoria": "preventa",
        "importancia": "critica",
        "etiqueta": ETIQUETAS["tienda"],
        "es_rumor": False,
        "estado": resultado["estado"],
        "precio": resultado.get("precio"),
        "moneda": resultado.get("moneda"),
        "titulo": resultado.get("titulo", ""),
        "envia_a_espana": fuente.get("envia_a_espana"),  # True, False o None (no se sabe)
        "primera_vez": estado_anterior is None,
    }


# ---------- Pruebas: python codigo/clasificador.py ----------

if __name__ == "__main__":
    from pathlib import Path
    from types import SimpleNamespace as Obj

    cfg = {
        "palabras_clave": {"es": ["naruto card game"], "en": ["naruto tcg"]},
        "exclusiones": ["mythos"],
        "preventa": {"es": ["preventa", "reservar"], "en": ["pre-order"]},
        "criticas": ["release date", "expansión"],
    }

    # Relevancia, exclusiones y tildes
    assert es_del_juego("Naruto TCG news", cfg)[0] is True
    assert es_del_juego("Naruto Mythos TCG set 2", cfg)[0] is False
    assert es_del_juego("Pokemon news", cfg)[0] is False
    assert es_critica("EXPANSION anunciada del Naruto Card Game", cfg) is True
    assert menciona_preventa("Ya puedes RESERVAR el juego", cfg) is True
    assert menciona_preventa("Un preorderable no cuenta", cfg) is False  # no empieza una palabra clave

    def noticia(titulo, resumen=""):
        return Obj(titulo=titulo, resumen=resumen)

    # Fuente oficial que menciona una preventa -> preventa crítica
    r = clasificar_elemento(noticia("Naruto Card Game pre-order opens"), {"tipo": "oficial"}, cfg)
    assert r["categoria"] == "preventa" and r["importancia"] == "critica"

    # Un medio que menciona preventa -> noticia, como mucho "alta"
    r = clasificar_elemento(noticia("Naruto Card Game pre-order rumored"), {"tipo": "medio"}, cfg)
    assert r["categoria"] == "noticia" and r["importancia"] == "alta"

    # Comunidad: rumor, como mucho "media"
    r = clasificar_elemento(noticia("Naruto TCG release date leaked"), {"tipo": "comunidad"}, cfg)
    assert r["importancia"] == "media" and r["es_rumor"] is True

    # Noticia normal de fuente oficial -> alta; no relevante -> None
    r = clasificar_elemento(noticia("Naruto Card Game new card art"), {"tipo": "oficial"}, cfg)
    assert r["categoria"] == "noticia" and r["importancia"] == "alta"
    assert clasificar_elemento(noticia("Pokemon pre-order"), {"tipo": "oficial"}, cfg) is None

    # Tiendas
    tienda = {"tipo": "tienda", "envia_a_espana": True}
    ficha = {"estado": "preventa", "titulo": "Naruto Card Game Booster", "precio": "5.00", "moneda": "EUR"}
    r = clasificar_tienda(ficha, tienda, cfg, None)
    assert r["categoria"] == "preventa" and r["primera_vez"] is True and r["envia_a_espana"] is True
    assert clasificar_tienda(ficha, tienda, cfg, "agotado") is not None
    assert clasificar_tienda(ficha, tienda, cfg, "preventa") is None          # no ha cambiado nada
    assert clasificar_tienda({**ficha, "estado": "desconocido"}, tienda, cfg, None) is None
    assert clasificar_tienda({**ficha, "estado": "agotado"}, tienda, cfg, None) is None
    ajena = {**ficha, "titulo": "Pokemon Booster"}
    assert clasificar_tienda(ajena, tienda, cfg, None) is None                # título ajeno al juego
    assert clasificar_tienda(ajena, {**tienda, "ficha_verificada": True}, cfg, None) is not None

    # Prueba con tu configuración real, si existe
    ruta = Path(__file__).resolve().parent.parent / "configuracion" / "configuracion.yml"
    if ruta.exists():
        import yaml
        real = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
        assert es_del_juego("NARUTO CARD GAME launches worldwide in June 2027", real)[0] is True
        assert es_del_juego("Naruto Mythos TCG Set 2 llega en septiembre", real)[0] is False
        assert menciona_preventa("Preventa del juego de cartas de Naruto", real) is True
        assert menciona_preventa("Naruto TCG: précommande ouverte", real) is True
        print("Prueba con configuracion.yml real OK")

    print("Clasificador OK")