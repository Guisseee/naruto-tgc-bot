"""Fiabilidad: peso de cada fuente, quién fue la primera en informar y quién resultó falsa."""
from almacenamiento import (
    fijar_peso,
    marcar_falso,
    obtener_fuentes,
    sumar_contador,
)
from clasificador import ETIQUETAS

BONUS_PRIMERA = 0.5       # puntos que gana una fuente al ser la primera en dar una noticia
PENALIZACION_FALSA = 8.0  # puntos que pierde cuando un elemento suyo resulta falso

# Banda (mínimo, máximo) del peso según el tipo de fuente
BANDAS = {
    "oficial": (70, 100),
    "distribuidor": (65, 100),
    "tienda": (40, 90),
    "medio": (25, 80),
    "comunidad": (5, 60),
}
BANDA_POR_DEFECTO = (5, 60)

UMBRAL_FALLOS = 5         # fallos seguidos a partir de los cuales se avisa al canal de estado
REPETIR_AVISO_CADA = 50   # y después se repite cada tantos fallos


def limitar_peso(peso, tipo):
    """Ajusta un peso para que quede dentro de la banda de su tipo de fuente."""
    minimo, maximo = BANDAS.get(tipo, BANDA_POR_DEFECTO)
    return max(minimo, min(maximo, peso))


def _obtener_fuente(conexion, fuente_id):
    return conexion.execute("SELECT * FROM fuentes WHERE id = ?", (fuente_id,)).fetchone()


def ajustar_peso(conexion, fuente_id, cambio):
    """Suma (o resta) puntos al peso de una fuente, respetando su banda. Devuelve el peso nuevo."""
    fuente = _obtener_fuente(conexion, fuente_id)
    if fuente is None:
        return None
    nuevo = limitar_peso(fuente["peso"] + cambio, fuente["tipo"])
    fijar_peso(conexion, fuente_id, nuevo)
    return nuevo


# ---------- Primera en informar ----------

def registrar_primera(conexion, fuente_id):
    """Anota que esta fuente fue la primera en dar una noticia (sube su contador y su peso)."""
    sumar_contador(conexion, fuente_id, "primeras")
    return ajustar_peso(conexion, fuente_id, BONUS_PRIMERA)


def registrar_nuevo_elemento(conexion, fuente_id, abre_grupo):
    """Llamar al guardar un elemento nuevo.

    Si el elemento abre un grupo propio (nadie había dado antes esa noticia), la fuente
    cuenta como la primera en informar. Si se une a un grupo que ya existía, no cuenta.
    """
    if abre_grupo:
        registrar_primera(conexion, fuente_id)


# ---------- Falsas o contradichas ----------

def registrar_falsa(conexion, elemento_id):
    """Marca un elemento como falso y penaliza a su fuente. Devuelve el id de la fuente."""
    fuente_id = marcar_falso(conexion, elemento_id)
    if fuente_id is None:
        return None
    sumar_contador(conexion, fuente_id, "falsas")
    ajustar_peso(conexion, fuente_id, -PENALIZACION_FALSA)
    return fuente_id


# ---------- Fuentes que dejan de funcionar ----------

def debe_avisar_fallo(fallos_seguidos, umbral=UMBRAL_FALLOS, repetir_cada=REPETIR_AVISO_CADA):
    """True si ahora toca avisar de que una fuente lleva demasiados fallos seguidos.

    Avisa al llegar exactamente al umbral y luego cada "repetir_cada" fallos más,
    para no llenar el canal de mensajes iguales.
    """
    if fallos_seguidos < umbral:
        return False
    return (fallos_seguidos - umbral) % repetir_cada == 0


# ---------- Tabla para mostrar ----------

def _veces(n):
    return "1 vez" if n == 1 else f"{n} veces"


def tabla_fiabilidad(conexion):
    """Texto con todas las fuentes, de más a menos fiable (para el comando de Discord)."""
    filas = obtener_fuentes(conexion)
    if not filas:
        return "No hay fuentes registradas."
    lineas = []
    for f in filas:
        etiqueta = ETIQUETAS.get(f["tipo"], ETIQUETAS["comunidad"])
        lineas.append(
            f"{etiqueta} · {f['nombre']}: peso {f['peso']:.1f} · "
            f"primera {_veces(f['primeras'])} · falsa {_veces(f['falsas'])}"
        )
    return "\n".join(lineas)


# ---------- Pruebas: python codigo/fiabilidad.py ----------

if __name__ == "__main__":
    from almacenamiento import conectar, guardar_elemento, registrar_fuentes

    bd = conectar(":memory:")
    registrar_fuentes(bd, [
        {"id": "oficial", "nombre": "Web oficial", "tipo": "oficial", "peso_inicial": 95},
        {"id": "rumor", "nombre": "Foro", "tipo": "comunidad", "peso_inicial": 30},
        {"id": "medio", "nombre": "Medio", "tipo": "medio", "peso_inicial": 60},
    ])

    def fila(fuente_id):
        return _obtener_fuente(bd, fuente_id)

    # Ser la primera suma al contador y al peso
    registrar_primera(bd, "medio")
    assert fila("medio")["primeras"] == 1
    assert fila("medio")["peso"] == 60.5

    # Si el elemento se une a un grupo que ya existía, no cuenta como primera
    registrar_nuevo_elemento(bd, "medio", abre_grupo=False)
    assert fila("medio")["primeras"] == 1
    registrar_nuevo_elemento(bd, "medio", abre_grupo=True)
    assert fila("medio")["primeras"] == 2

    # El peso nunca pasa del máximo de su banda
    for _ in range(200):
        registrar_primera(bd, "rumor")
    assert fila("rumor")["peso"] == 60

    # Una falsa resta y cuenta (95 - 8 = 87)
    id1 = guardar_elemento(bd, "h1", "oficial", "Titular", "https://e.com/1", "en", "noticia", "alta")
    assert registrar_falsa(bd, id1) == "oficial"
    assert fila("oficial")["falsas"] == 1
    assert fila("oficial")["peso"] == 87

    # Y el peso nunca baja del mínimo de su banda
    for i in range(30):
        n = guardar_elemento(bd, f"x{i}", "oficial", f"Titular {i}", f"https://e.com/x{i}", "en", "noticia", "alta")
        registrar_falsa(bd, n)
    assert fila("oficial")["peso"] == 70

    # Fuente que no existe: no hace nada y no da error
    assert ajustar_peso(bd, "no_existe", 5) is None

    # Avisos de fallos seguidos
    assert debe_avisar_fallo(4) is False
    assert debe_avisar_fallo(5) is True
    assert debe_avisar_fallo(6) is False
    assert debe_avisar_fallo(55) is True

    # La tabla muestra las fuentes de más a menos fiable
    texto = tabla_fiabilidad(bd)
    assert "Web oficial" in texto and "Foro" in texto and "Medio" in texto
    assert texto.index("Web oficial") < texto.index("Foro")

    print("Fiabilidad OK")