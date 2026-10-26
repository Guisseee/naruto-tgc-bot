"""Descarga de páginas respetando robots.txt, pausas por dominio y reintentos."""
import sys
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

# Nombre con el que el bot se identifica ante las webs (no finge ser un navegador)
TOKEN_AGENTE = "NarutoTCGAvisosBot"
AGENTE_USUARIO = f"{TOKEN_AGENTE}/1.0 (bot personal de avisos, solo lectura)"

SEGUNDOS_CACHE_ROBOTS = 3600  # el robots.txt de cada dominio se relee cada hora
CODIGOS_REINTENTABLES = (429, 500, 502, 503, 504)


class BloqueadoPorRobots(Exception):
    """El robots.txt del sitio no permite descargar esa dirección."""


class ErrorDescarga(Exception):
    """No se pudo descargar la página (tras los reintentos)."""


class Descargador:
    def __init__(self, pausa_segundos=5, reintentos=3, timeout=20):
        self.pausa = pausa_segundos
        self.reintentos = reintentos
        self.cliente = httpx.Client(
            headers={
                "User-Agent": AGENTE_USUARIO,
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            },
            timeout=timeout,
            follow_redirects=True,
        )
        self._ultima_peticion = {}  # dominio -> instante de la última petición
        self._robots = {}           # dominio -> (reglas, instante en que se leyeron)

    # ---------- Pausas por dominio ----------

    def _esperar_turno(self, dominio):
        """Espera lo necesario para no pedir al mismo dominio más rápido que la pausa."""
        ultima = self._ultima_peticion.get(dominio)
        if ultima is not None:
            falta = self.pausa - (time.monotonic() - ultima)
            if falta > 0:
                time.sleep(falta)
        self._ultima_peticion[dominio] = time.monotonic()

    # ---------- robots.txt ----------

    def _reglas_robots(self, url):
        """Devuelve las reglas de robots.txt del dominio de la url (con caché)."""
        partes = urlsplit(url)
        dominio = partes.netloc.lower()

        guardado = self._robots.get(dominio)
        if guardado and time.monotonic() - guardado[1] < SEGUNDOS_CACHE_ROBOTS:
            return guardado[0]

        direccion = f"{partes.scheme}://{partes.netloc}/robots.txt"
        self._esperar_turno(dominio)
        try:
            respuesta = self.cliente.get(direccion)
        except httpx.HTTPError:
            raise ErrorDescarga(f"No se pudo leer el robots.txt de {dominio}") from None

        reglas = RobotFileParser()
        if respuesta.status_code == 200:
            reglas.parse(respuesta.text.splitlines())
        elif respuesta.status_code in (401, 403):
            # Acceso prohibido: lo más prudente es tratar todo el sitio como vetado
            reglas.parse(["User-agent: *", "Disallow: /"])
        elif respuesta.status_code == 429 or respuesta.status_code >= 500:
            # Error temporal: mejor no descargar nada que arriesgarse a saltarse las reglas
            raise ErrorDescarga(
                f"robots.txt de {dominio} respondió con el código {respuesta.status_code}"
            )
        else:
            # 404 u otro: el sitio no tiene robots.txt, sin restricciones
            reglas.parse(["User-agent: *", "Allow: /"])

        self._robots[dominio] = (reglas, time.monotonic())
        return reglas

    def permitido(self, url):
        """True si el robots.txt del sitio permite que este bot lea esa dirección."""
        return self._reglas_robots(url).can_fetch(TOKEN_AGENTE, url)

    # ---------- Descarga ----------

    @staticmethod
    def _espera_indicada(respuesta):
        """Segundos que pide esperar el servidor (cabecera Retry-After), o 0."""
        valor = respuesta.headers.get("Retry-After", "")
        return int(valor) if valor.isdigit() else 0

    def descargar(self, url):
        """Descarga la página y devuelve su texto.

        Lanza BloqueadoPorRobots si el sitio no lo permite, y ErrorDescarga si falla.
        """
        if not self.permitido(url):
            raise BloqueadoPorRobots(f"robots.txt no permite leer {url}")

        dominio = urlsplit(url).netloc.lower()
        ultimo_error = "motivo desconocido"

        for intento in range(1, self.reintentos + 1):
            self._esperar_turno(dominio)
            try:
                respuesta = self.cliente.get(url)
            except httpx.HTTPError as error:
                ultimo_error = f"error de red ({type(error).__name__})"
            else:
                if respuesta.status_code == 200:
                    return respuesta.text
                if respuesta.status_code in CODIGOS_REINTENTABLES:
                    ultimo_error = f"código {respuesta.status_code}"
                    time.sleep(min(self._espera_indicada(respuesta), 60))
                else:
                    raise ErrorDescarga(
                        f"{url} respondió con el código {respuesta.status_code}"
                    )

            if intento < self.reintentos:
                time.sleep(2 ** intento)  # espera creciente: 2 s, 4 s...

        raise ErrorDescarga(f"No se pudo descargar {url}: {ultimo_error}")

    def cerrar(self):
        self.cliente.close()


# ---------- Pruebas: python codigo/descargador.py [direccion] ----------

if __name__ == "__main__":
    # Prueba 1: las reglas de robots.txt se aplican (sin usar internet)
    d = Descargador(pausa_segundos=1)
    reglas = RobotFileParser()
    reglas.parse(["User-agent: *", "Disallow: /privado/"])
    d._robots["ejemplo.com"] = (reglas, time.monotonic())
    assert d.permitido("https://ejemplo.com/publico/pagina") is True
    assert d.permitido("https://ejemplo.com/privado/pagina") is False

    # Prueba 2: la pausa se aplica al mismo dominio, pero no a otro distinto
    d._esperar_turno("a.com")
    inicio = time.monotonic()
    d._esperar_turno("a.com")
    assert time.monotonic() - inicio >= 0.9, "Debía esperar la pausa en el mismo dominio"
    inicio = time.monotonic()
    d._esperar_turno("b.com")
    assert time.monotonic() - inicio < 0.5, "No debía esperar en un dominio distinto"
    print("Pruebas sin internet OK")

    # Prueba 3 (opcional): descarga real, si pasas una dirección como argumento
    if len(sys.argv) > 1:
        direccion = sys.argv[1]
        try:
            texto = d.descargar(direccion)
            print(f"Descarga OK: {len(texto)} caracteres")
            print(texto[:150].replace("\n", " "))
        except (BloqueadoPorRobots, ErrorDescarga) as error:
            print(f"No se pudo descargar: {error}")

    d.cerrar()