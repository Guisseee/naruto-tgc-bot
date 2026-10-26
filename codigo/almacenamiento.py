"""Base de datos SQLite del bot: historial, fuentes y estado de las tiendas."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

RUTA_POR_DEFECTO = "datos/bot.db"

# Orden de importancia, de más a menos (se usa para ordenar los resúmenes)
ORDEN_IMPORTANCIA = "CASE importancia WHEN 'critica' THEN 0 WHEN 'alta' THEN 1 WHEN 'media' THEN 2 ELSE 3 END"

CAMPOS_CONTADOR = {"primeras", "falsas"}


def ahora():
    """Fecha y hora actuales en UTC, como texto ISO (se pueden ordenar como texto)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conectar(ruta=RUTA_POR_DEFECTO):
    """Abre la base de datos (la crea si no existe) y prepara las tablas."""
    if ruta != ":memory:":
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
    conexion = sqlite3.connect(ruta)
    conexion.row_factory = sqlite3.Row
    inicializar(conexion)
    return conexion


def inicializar(conexion):
    """Crea las tablas si todavía no existen."""
    conexion.executescript(
        """
        CREATE TABLE IF NOT EXISTS fuentes (
            id TEXT PRIMARY KEY,
            nombre TEXT NOT NULL,
            tipo TEXT NOT NULL,
            peso REAL NOT NULL,
            primeras INTEGER NOT NULL DEFAULT 0,
            falsas INTEGER NOT NULL DEFAULT 0,
            fallos_seguidos INTEGER NOT NULL DEFAULT 0,
            ultimo_ok TEXT
        );

        CREATE TABLE IF NOT EXISTS elementos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            huella TEXT NOT NULL UNIQUE,
            fuente_id TEXT NOT NULL,
            titulo TEXT NOT NULL,
            url TEXT,
            idioma TEXT,
            categoria TEXT NOT NULL,
            importancia TEXT NOT NULL,
            resumen TEXT,
            grupo TEXT NOT NULL,
            detectado_en TEXT NOT NULL,
            enviado INTEGER NOT NULL DEFAULT 0,
            falso INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS estados_tienda (
            url TEXT PRIMARY KEY,
            estado TEXT NOT NULL,
            precio TEXT,
            actualizado_en TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS metadatos (
            clave TEXT PRIMARY KEY,
            valor TEXT
        );
        """
    )
    conexion.commit()


# ---------- Fuentes ----------

def registrar_fuentes(conexion, fuentes):
    """Da de alta las fuentes de fuentes.yml. Si ya existen, no toca su peso ni sus contadores."""
    with conexion:
        for f in fuentes:
            conexion.execute(
                """
                INSERT INTO fuentes (id, nombre, tipo, peso)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET nombre = excluded.nombre, tipo = excluded.tipo
                """,
                (f["id"], f["nombre"], f["tipo"], float(f.get("peso_inicial", 50))),
            )


def obtener_fuentes(conexion):
    """Devuelve todas las fuentes, de más a menos peso."""
    return conexion.execute("SELECT * FROM fuentes ORDER BY peso DESC").fetchall()


def sumar_contador(conexion, fuente_id, campo):
    """Suma 1 a 'primeras' (fue la primera en dar una noticia) o 'falsas' (resultó falsa)."""
    if campo not in CAMPOS_CONTADOR:
        raise ValueError(f"Campo no válido: {campo}")
    with conexion:
        conexion.execute(f"UPDATE fuentes SET {campo} = {campo} + 1 WHERE id = ?", (fuente_id,))


def fijar_peso(conexion, fuente_id, peso):
    """Cambia el peso (fiabilidad) de una fuente, siempre entre 0 y 100."""
    peso = max(0.0, min(100.0, float(peso)))
    with conexion:
        conexion.execute("UPDATE fuentes SET peso = ? WHERE id = ?", (peso, fuente_id))


def registrar_resultado_fuente(conexion, fuente_id, ok):
    """Anota si la última consulta a la fuente funcionó. Devuelve los fallos seguidos."""
    with conexion:
        if ok:
            conexion.execute(
                "UPDATE fuentes SET fallos_seguidos = 0, ultimo_ok = ? WHERE id = ?",
                (ahora(), fuente_id),
            )
        else:
            conexion.execute(
                "UPDATE fuentes SET fallos_seguidos = fallos_seguidos + 1 WHERE id = ?",
                (fuente_id,),
            )
    fila = conexion.execute(
        "SELECT fallos_seguidos FROM fuentes WHERE id = ?", (fuente_id,)
    ).fetchone()
    return fila["fallos_seguidos"] if fila else 0


# ---------- Elementos (noticias y preventas detectadas) ----------

def guardar_elemento(conexion, huella, fuente_id, titulo, url, idioma,
                     categoria, importancia, resumen="", grupo=None):
    """Guarda un elemento nuevo. Devuelve su id, o None si ya existía (duplicado)."""
    with conexion:
        cursor = conexion.execute(
            """
            INSERT OR IGNORE INTO elementos
                (huella, fuente_id, titulo, url, idioma, categoria,
                 importancia, resumen, grupo, detectado_en)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (huella, fuente_id, titulo, url, idioma, categoria,
             importancia, resumen, grupo or huella, ahora()),
        )
    if cursor.rowcount == 0:
        return None
    return cursor.lastrowid


def existe_huella(conexion, huella):
    """Dice si ya se ha visto un elemento con esa huella."""
    fila = conexion.execute("SELECT 1 FROM elementos WHERE huella = ?", (huella,)).fetchone()
    return fila is not None


def marcar_enviado(conexion, elemento_id):
    with conexion:
        conexion.execute("UPDATE elementos SET enviado = 1 WHERE id = ?", (elemento_id,))


def marcar_falso(conexion, elemento_id):
    """Marca un elemento como falso y devuelve el id de su fuente (para restarle fiabilidad)."""
    with conexion:
        conexion.execute("UPDATE elementos SET falso = 1 WHERE id = ?", (elemento_id,))
    fila = conexion.execute(
        "SELECT fuente_id FROM elementos WHERE id = ?", (elemento_id,)
    ).fetchone()
    return fila["fuente_id"] if fila else None


def elementos_sin_enviar(conexion, categoria=None):
    """Elementos pendientes de enviar (para los resúmenes), de más a menos importantes."""
    consulta = "SELECT * FROM elementos WHERE enviado = 0 AND falso = 0"
    parametros = []
    if categoria:
        consulta += " AND categoria = ?"
        parametros.append(categoria)
    consulta += f" ORDER BY {ORDEN_IMPORTANCIA}, detectado_en"
    return conexion.execute(consulta, parametros).fetchall()


def elementos_desde(conexion, desde):
    """Elementos detectados desde una fecha ISO (para el recap tras estar apagado)."""
    return conexion.execute(
        f"SELECT * FROM elementos WHERE detectado_en >= ? AND falso = 0 "
        f"ORDER BY {ORDEN_IMPORTANCIA}, detectado_en",
        (desde,),
    ).fetchall()


# ---------- Estado de las tiendas ----------

def obtener_estado_tienda(conexion, url):
    """Último estado guardado de una ficha de tienda, o None si es la primera vez."""
    return conexion.execute("SELECT * FROM estados_tienda WHERE url = ?", (url,)).fetchone()


def guardar_estado_tienda(conexion, url, estado, precio=None):
    with conexion:
        conexion.execute(
            """
            INSERT INTO estados_tienda (url, estado, precio, actualizado_en)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                estado = excluded.estado,
                precio = excluded.precio,
                actualizado_en = excluded.actualizado_en
            """,
            (url, estado, precio, ahora()),
        )


# ---------- Metadatos (por ejemplo, cuándo fue la última ejecución) ----------

def leer_metadato(conexion, clave, defecto=None):
    fila = conexion.execute("SELECT valor FROM metadatos WHERE clave = ?", (clave,)).fetchone()
    return fila["valor"] if fila else defecto


def guardar_metadato(conexion, clave, valor):
    with conexion:
        conexion.execute(
            """
            INSERT INTO metadatos (clave, valor) VALUES (?, ?)
            ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor
            """,
            (clave, valor),
        )


# ---------- Prueba rápida: python codigo/almacenamiento.py ----------

if __name__ == "__main__":
    bd = conectar(":memory:")  # base de datos temporal, en memoria

    registrar_fuentes(bd, [
        {"id": "prueba", "nombre": "Fuente de prueba", "tipo": "oficial", "peso_inicial": 90}
    ])
    assert len(obtener_fuentes(bd)) == 1

    nuevo = guardar_elemento(bd, "abc123", "prueba", "Título", "https://ejemplo.com",
                             "es", "noticia", "alta")
    repetido = guardar_elemento(bd, "abc123", "prueba", "Título", "https://ejemplo.com",
                                "es", "noticia", "alta")
    assert nuevo is not None, "El primer elemento debía guardarse"
    assert repetido is None, "El duplicado debía rechazarse"

    assert len(elementos_sin_enviar(bd)) == 1
    marcar_enviado(bd, nuevo)
    assert len(elementos_sin_enviar(bd)) == 0

    sumar_contador(bd, "prueba", "primeras")
    assert obtener_fuentes(bd)[0]["primeras"] == 1

    assert registrar_resultado_fuente(bd, "prueba", ok=False) == 1
    assert registrar_resultado_fuente(bd, "prueba", ok=True) == 0

    assert obtener_estado_tienda(bd, "https://tienda.ejemplo/ficha") is None
    guardar_estado_tienda(bd, "https://tienda.ejemplo/ficha", "agotado")
    assert obtener_estado_tienda(bd, "https://tienda.ejemplo/ficha")["estado"] == "agotado"

    guardar_metadato(bd, "ultima_ejecucion", "2026-10-26T12:00:00+00:00")
    assert leer_metadato(bd, "ultima_ejecucion") == "2026-10-26T12:00:00+00:00"

    print("Almacenamiento OK")