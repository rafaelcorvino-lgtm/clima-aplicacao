#!/usr/bin/env python3
"""Cifra os dados publicados no GitHub Pages com a senha da pagina.

O repositorio e publico; o que sai cifrado so abre com a senha. A pagina
decifra no navegador (WebCrypto) com EXATAMENTE os mesmos parametros:

    chave  = PBKDF2-HMAC-SHA256(senha, sal de 16 bytes, 200000 iteracoes) -> 32 bytes
    cifra  = AES-256-GCM, iv de 12 bytes, sem dado adicional
    arquivo = JSON {"v": 1, "salt": b64, "iv": b64, "ct": b64}

Precisa do pacote 'cryptography' (so no robo da nuvem; o notebook nao usa).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ITERACOES = 200_000


def _chave(senha: str, sal: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), sal, ITERACOES, dklen=32)


def cifrar(dados: bytes, senha: str) -> bytes:
    sal = os.urandom(16)
    iv = os.urandom(12)
    ct = AESGCM(_chave(senha, sal)).encrypt(iv, dados, None)
    return json.dumps({"v": 1, "salt": _b64(sal), "iv": _b64(iv), "ct": _b64(ct)}).encode()


def decifrar(arquivo: bytes, senha: str) -> bytes:
    d = json.loads(arquivo.decode())
    sal, iv, ct = (base64.b64decode(d[k]) for k in ("salt", "iv", "ct"))
    return AESGCM(_chave(senha, sal)).decrypt(iv, ct, None)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


if __name__ == "__main__":
    s = "teste"
    c = cifrar(b"ola mundo", s)
    assert decifrar(c, s) == b"ola mundo"
    try:
        decifrar(c, "errada")
        raise SystemExit("deveria ter falhado")
    except Exception:
        print("cifra ok")
