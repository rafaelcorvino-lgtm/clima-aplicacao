#!/usr/bin/env python3
"""Publica o Clima de Aplicacao na nuvem (Firebase, projeto calendario-aerea-26140).

    python aplicacao-aerea/publicar.py             copia + deploy (funcoes e site)
    python aplicacao-aerea/publicar.py --so-site   so a pagina (rapido)
    python aplicacao-aerea/publicar.py --so-copiar so copia os arquivos, sem deploy

O que faz:
  1. copia zeus.py, regras.py, previsao.py, estado.py e config.json para
     firebase/clima/ (a funcao usa EXATAMENTE o codigo do notebook);
  2. copia pagina.html para firebase/clima-site/index.html;
  3. roda firebase deploy.

Os segredos (login Zeus e senha da pagina) ficam no Secret Manager do Google
e sao enviados UMA vez, pela mao do dono, na pasta firebase/:

    firebase functions:secrets:set ZEUS_EMAIL
    firebase functions:secrets:set ZEUS_SENHA
    firebase functions:secrets:set APP_SENHA

(cada comando pergunta o valor; cole o mesmo que esta em segredos.txt)

Endereco final: https://clima-aplicacao.web.app
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
FB = os.path.join(RAIZ, "firebase")
FUNC = os.path.join(FB, "clima")
SITE = os.path.join(FB, "clima-site")
PROJETO = "calendario-aerea-26140"
MODULOS = ["zeus.py", "regras.py", "previsao.py", "estado.py", "config.json"]


def copiar():
    os.makedirs(FUNC, exist_ok=True)
    os.makedirs(SITE, exist_ok=True)
    for m in MODULOS:
        shutil.copyfile(os.path.join(AQUI, m), os.path.join(FUNC, m))
    shutil.copyfile(os.path.join(AQUI, "pagina.html"), os.path.join(SITE, "index.html"))
    print("copiado:", ", ".join(MODULOS), "e pagina.html -> index.html")


def firebase(*args: str) -> int:
    cmd = ["firebase", *args, "--project", PROJETO]
    print(">", " ".join(cmd))
    return subprocess.call(cmd, cwd=FB, shell=(os.name == "nt"))


def main():
    copiar()
    if "--so-copiar" in sys.argv:
        return
    if "--so-site" in sys.argv:
        rc = firebase("deploy", "--only", "hosting:clima")
    else:
        rc = firebase("deploy", "--only", "functions:clima,hosting:clima")
    if rc == 0:
        print("\nPublicado: https://clima-aplicacao.web.app")
    sys.exit(rc)


if __name__ == "__main__":
    main()
