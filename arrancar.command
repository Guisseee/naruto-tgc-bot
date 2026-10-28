#!/bin/bash
# Arranca el bot en tu ordenador (modo --daemon).
# Se queda funcionando hasta que lo pares con Ctrl+C.

# Entra en la carpeta del proyecto (las comillas son necesarias si la ruta tiene espacios)
cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "No encuentro el entorno virtual (.venv)."
  echo "Créalo con:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  read -r -p "Pulsa Enter para cerrar..." _
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "Falta el archivo .env con tus webhooks de Discord."
  echo "Créalo copiando .env.example y rellenándolo."
  read -r -p "Pulsa Enter para cerrar..." _
  exit 1
fi

echo "Arrancando el bot. Para pararlo: Ctrl+C."
# caffeinate evita que el Mac se duerma por inactividad mientras el bot funciona
caffeinate -i .venv/bin/python codigo/principal.py --daemon

echo "El bot se ha detenido."
read -r -p "Pulsa Enter para cerrar..." _