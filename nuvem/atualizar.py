#!/usr/bin/env python3
"""Robo da nuvem (GitHub Actions): monta o estado e publica cifrado no Pages.

    python nuvem/atualizar.py --saida site [--esperar-quadro]

Le ZEUS_EMAIL, ZEUS_SENHA e APP_SENHA do ambiente (segredos do GitHub).
Na pasta --saida (o checkout do ramo gh-pages) escreve:

    index.html        a pagina (copia de pagina.html)
    estado.enc        o estado completo, cifrado com APP_SENHA
    historico.db.enc  o historico (SQLite) cifrado; e lido e reescrito a cada vez
    meta.json         so a hora da ultima atualizacao (sem dado sensivel)

--esperar-quadro: o mecanismo dos "primeiros minutos". A Zeus fecha um quadro
por hora e ele chega por volta de hh:02. Se o robo rodou nos primeiros 25
minutos da hora e o quadro novo ainda nao apareceu, ele tenta de novo a cada
60 s (ate 15 vezes) e so publica quando o quadro da hora chegou -- assim a
pagina mostra a hora nova minutos depois de fechada, mesmo que o GitHub
atrase a disparada.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
sys.path.insert(0, RAIZ)
sys.path.insert(0, AQUI)

import cifra  # noqa: E402
import estado as mod_estado  # noqa: E402
import historico as mod_historico  # noqa: E402
import zeus  # noqa: E402


def quadro_mais_novo(est: dict) -> datetime | None:
    horas = []
    for f in est.get("fazendas", []):
        for s in f.get("estacoes", []):
            h = (s.get("leitura") or {}).get("hora")
            d = mod_estado._dt(h)
            if d:
                horas.append(d)
    return max(horas) if horas else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", required=True)
    ap.add_argument("--esperar-quadro", action="store_true")
    ap.add_argument("--max-tentativas", type=int, default=15)
    args = ap.parse_args()

    email, senha, app_senha = (os.environ.get(k, "") for k in ("ZEUS_EMAIL", "ZEUS_SENHA", "APP_SENHA"))
    if not email or not senha or not app_senha:
        print("Faltam ZEUS_EMAIL / ZEUS_SENHA / APP_SENHA no ambiente (segredos do GitHub).")
        sys.exit(1)

    os.makedirs(args.saida, exist_ok=True)
    with open(os.path.join(RAIZ, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    # historico: decifra o que esta publicado, usa, e cifra de volta no fim
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "historico.db")
    enc = os.path.join(args.saida, "historico.db.enc")
    if os.path.exists(enc):
        try:
            with open(enc, "rb") as f:
                open(db, "wb").write(cifra.decifrar(f.read(), app_senha))
        except Exception as e:  # noqa: BLE001
            print(f"AVISO: nao consegui abrir o historico publicado ({e}); comecando um novo")
    historico = mod_historico.Historico(db)
    cliente = zeus.ClienteZeus(email, senha, os.path.join(tmp, "sessao.json"))
    cache = mod_estado.CachePrevisao()

    est = None
    for tentativa in range(1, args.max_tentativas + 1):
        est = mod_estado.montar(cfg, cliente, False, cache, historico)
        agora = datetime.now(timezone.utc)
        novo = quadro_mais_novo(est)
        hora_atual = agora.replace(minute=0, second=0, microsecond=0)
        print(f"tentativa {tentativa}: {est['total_estacoes']} estacoes, quadro mais novo "
              f"{novo.isoformat() if novo else None}, agora {agora:%H:%M} UTC, avisos={len(est['avisos'])}")
        if not args.esperar_quadro or est["total_estacoes"] == 0:
            break
        if novo is not None and novo >= hora_atual:
            break  # o quadro desta hora ja chegou
        if agora.minute >= 25:
            print("passou dos 25 min da hora; publicando o que ha")
            break
        print("quadro da hora ainda nao fechou; esperando 60 s")
        time.sleep(60)

    # escreve a saida
    with open(os.path.join(args.saida, "estado.enc"), "wb") as f:
        f.write(cifra.cifrar(json.dumps(est, ensure_ascii=False).encode(), app_senha))
    historico._con.commit()
    with open(db, "rb") as f:
        dados_db = f.read()
    with open(enc, "wb") as f:
        f.write(cifra.cifrar(dados_db, app_senha))
    with open(os.path.join(args.saida, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"atualizado_em": est["atualizado_em"], "quadro": (quadro_mais_novo(est) or datetime.min).isoformat(),
                   "fazendas": len(est["fazendas"]), "estacoes": est["total_estacoes"]}, f)
    shutil.copyfile(os.path.join(RAIZ, "pagina.html"), os.path.join(args.saida, "index.html"))
    with open(os.path.join(args.saida, ".nojekyll"), "w") as f:
        f.write("")
    shutil.rmtree(tmp, ignore_errors=True)
    print("publicado:", est["atualizado_em"], est["resumo"])
    for a in est["avisos"][:10]:
        print("  aviso:", a)


if __name__ == "__main__":
    main()
