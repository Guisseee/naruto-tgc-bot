"""Punto de entrada del bot."""
import argparse
import os
from pathlib import Path

from avisos import enviar_aviso


def cargar_env(ruta=".env"):
    """Lee el archivo .env (si existe) y carga sus variables.

    En GitHub Actions no hay .env: allí las variables llegan como Secrets.
    """
    archivo = Path(ruta)
    if not archivo.exists():
        return
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip())


def main():
    parser = argparse.ArgumentParser(description="Bot de avisos del Naruto Card Game")
    parser.add_argument("--prueba", action="store_true", help="Envía un aviso de prueba")
    args = parser.parse_args()

    cargar_env()

    if args.prueba:
        enviar_aviso(
            canal="estado",
            titulo="✅ Prueba de conexión",
            descripcion="Si ves este mensaje, el bot ya puede escribir en Discord.",
            tipo="estado",
        )
        print("Aviso de prueba enviado. Mira el canal #estado-bot.")


if __name__ == "__main__":
    main()