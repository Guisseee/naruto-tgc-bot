"""Envío de avisos a Discord mediante webhooks."""
import os

import httpx

# Colores de las tarjetas (embeds) de Discord, en hexadecimal
COLORES = {
    "preventa": 0xE74C3C,  # rojo, para que destaque
    "noticia": 0x3498DB,   # azul
    "estado": 0x95A5A6,    # gris
}


def enviar_aviso(canal, titulo, descripcion, tipo="estado", mencion=None):
    """Envía una tarjeta al canal indicado ("preventas", "noticias" o "estado").

    La dirección del webhook se lee de la variable de entorno
    DISCORD_WEBHOOK_<CANAL>. Nunca se escribe en el código.
    """
    variable = f"DISCORD_WEBHOOK_{canal.upper()}"
    url = os.environ.get(variable)
    if not url:
        raise RuntimeError(f"Falta la variable {variable}. Revisa tu archivo .env")

    carga = {
        "embeds": [
            {
                "title": titulo[:256],
                "description": descripcion[:4000],
                "color": COLORES.get(tipo, COLORES["estado"]),
            }
        ]
    }
    if mencion:
        carga["content"] = mencion

    # Se capturan los errores para no mostrar nunca la URL del webhook en pantalla
    try:
        respuesta = httpx.post(url, json=carga, timeout=15)
        respuesta.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise RuntimeError(
            f"Discord respondió con el código {error.response.status_code}"
        ) from None
    except httpx.HTTPError:
        raise RuntimeError("No se pudo conectar con Discord") from None