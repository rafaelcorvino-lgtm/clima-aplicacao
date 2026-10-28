#!/usr/bin/env python3
"""Robo da nuvem (GitHub Actions): monta o estado e publica cifrado no Pages.

    python nuvem/atualizar.py --saida site                      uma vez
    python nuvem/atualizar.py --saida site --laco 350 --push    em laco, por 350 min

Le ZEUS_EMAIL, ZEUS_SENHA e APP_SENHA do ambiente (segredos do GitHub).
Na pasta --saida (o checkout do ramo gh-pages) escreve:

    index.html        a pagina (copia de pagina.html)
    estado.enc        o estado completo, cifrado com APP_SENHA
    historico.db.enc  o historico (SQLite) cifrado; e lido e reescrito a cada vez
    meta.json         so a hora da ultima atualizacao (sem dado sensivel)

POR QUE LACO: o agendamento (cron) do GitHub e pouco confiavel -- medido em
23 a 25/10/2026, disparou ~7 vezes por dia em vez de 144. Entao uma execucao
fica viva por quase 6 h (o maximo de um job) publicando a cada 10 min, e o
cron so serve para reiniciar o laco; com sobreposicao (concurrency), um
atraso de horas no cron nao abre buraco.

MECANISMO DOS PRIMEIROS MINUTOS: a Zeus fecha um quadro por hora e ele chega
por volta de hh:02. O laco acorda em :02, :12, :22, :32, :42 e :52. Nos
primeiros 25 min da hora, se o quadro novo ainda nao apareceu, tenta de novo
a cada 60 s ate ele chegar; so entao publica.

--push: depois de cada publicacao faz git add/commit/push na pasta --saida.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
sys.path.insert(0, RAIZ)
sys.path.insert(0, AQUI)

import cifra  # noqa: E402
import estado as mod_estado  # noqa: E402
import historico as mod_historico  # noqa: E402
import zeus  # noqa: E402

MINUTOS_DE_ACORDAR = (2, 12, 22, 32, 42, 52)


def log(msg: str):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] {msg}", flush=True)


def quadro_mais_novo(est: dict) -> datetime | None:
    horas = []
    for f in est.get("fazendas", []):
        for s in f.get("estacoes", []):
            d = mod_estado._dt((s.get("leitura") or {}).get("hora"))
            if d:
                horas.append(d)
    return max(horas) if horas else None


class Robo:
    def __init__(self, saida: str, email: str, senha: str, app_senha: str, push: bool, aberto: bool = False):
        self.saida = saida
        self.app_senha = app_senha
        self.push = push
        self.aberto = aberto  # True = publica sem senha (estado.json e historico.db abertos)
        os.makedirs(saida, exist_ok=True)
        with open(os.path.join(RAIZ, "config.json"), encoding="utf-8") as f:
            self.cfg = json.load(f)
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "historico.db")
        # historico publicado: aberto (historico.db) ou cifrado (historico.db.enc)
        aberto_pub = os.path.join(saida, "historico.db")
        enc = os.path.join(saida, "historico.db.enc")
        if os.path.exists(aberto_pub):
            shutil.copyfile(aberto_pub, self.db)
        elif os.path.exists(enc) and app_senha:
            try:
                with open(enc, "rb") as f:
                    open(self.db, "wb").write(cifra.decifrar(f.read(), app_senha))
            except Exception as e:  # noqa: BLE001
                log(f"AVISO: nao consegui abrir o historico publicado ({e}); comecando um novo")
        self.historico = mod_historico.Historico(self.db)
        self.cliente = zeus.ClienteZeus(email, senha, os.path.join(self.tmp, "sessao.json"))
        self.cache = mod_estado.CachePrevisao()

    def montar_esperando(self, esperar: bool, max_tentativas: int = 15) -> dict:
        est = None
        for tentativa in range(1, max_tentativas + 1):
            est = mod_estado.montar(self.cfg, self.cliente, False, self.cache, self.historico)
            agora = datetime.now(timezone.utc)
            novo = quadro_mais_novo(est)
            hora_atual = agora.replace(minute=0, second=0, microsecond=0)
            log(f"tentativa {tentativa}: {est['total_estacoes']} estacoes, quadro mais novo "
                f"{novo.isoformat() if novo else None}, avisos={len(est['avisos'])}")
            if not esperar or est["total_estacoes"] == 0:
                break
            if novo is not None and novo >= hora_atual:
                break  # o quadro desta hora ja chegou
            if agora.minute >= 25:
                log("passou dos 25 min da hora; publicando o que ha")
                break
            log("quadro da hora ainda nao fechou; esperando 60 s")
            time.sleep(60)
        return est

    def escrever(self, est: dict):
        self.historico._con.commit()
        with open(self.db, "rb") as f:
            dados_db = f.read()
        if self.aberto:
            # Sem senha: estado.json e historico.db abertos; apaga as versoes cifradas.
            with open(os.path.join(self.saida, "estado.json"), "w", encoding="utf-8") as f:
                json.dump(est, f, ensure_ascii=False)
            with open(os.path.join(self.saida, "historico.db"), "wb") as f:
                f.write(dados_db)
            for velho in ("estado.enc", "historico.db.enc"):
                try:
                    os.remove(os.path.join(self.saida, velho))
                except FileNotFoundError:
                    pass
        else:
            with open(os.path.join(self.saida, "estado.enc"), "wb") as f:
                f.write(cifra.cifrar(json.dumps(est, ensure_ascii=False).encode(), self.app_senha))
            with open(os.path.join(self.saida, "historico.db.enc"), "wb") as f:
                f.write(cifra.cifrar(dados_db, self.app_senha))
            for velho in ("estado.json", "historico.db"):
                try:
                    os.remove(os.path.join(self.saida, velho))
                except FileNotFoundError:
                    pass
        with open(os.path.join(self.saida, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"atualizado_em": est["atualizado_em"],
                       "quadro": (quadro_mais_novo(est) or datetime.min).isoformat(),
                       "fazendas": len(est["fazendas"]), "estacoes": est["total_estacoes"]}, f)
        shutil.copyfile(os.path.join(RAIZ, "pagina.html"), os.path.join(self.saida, "index.html"))
        with open(os.path.join(self.saida, ".nojekyll"), "w") as f:
            f.write("")
        log(f"publicado: {est['atualizado_em']} {est['resumo']}")
        for a in est["avisos"][:10]:
            log(f"  aviso: {a}")

    def git_push(self):
        if not self.push:
            return

        def git(*args):
            return subprocess.run(["git", *args], cwd=self.saida, capture_output=True, text=True)

        git("add", "-A")
        if git("diff", "--cached", "--quiet").returncode == 0:
            log("git: nada mudou")
            return
        r = git("commit", "-q", "-m", f"atualiza {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
        if r.returncode != 0:
            log(f"git commit falhou: {r.stderr.strip()[:300]}")
            return
        for tentativa in range(3):
            r = git("push", "-q")
            if r.returncode == 0:
                log("git: publicado no gh-pages")
                return
            log(f"git push falhou ({r.stderr.strip()[:200]}); tentando rebase")
            git("pull", "--rebase", "-q")
        log("git: desisti do push nesta rodada")

    def rodar_uma_vez(self, esperar: bool):
        try:
            est = self.montar_esperando(esperar)
            self.escrever(est)
            self.git_push()
        except Exception:  # noqa: BLE001
            log("ERRO nesta rodada (o laco continua):")
            traceback.print_exc()


def proximo_despertar(agora: datetime) -> datetime:
    base = agora.replace(second=0, microsecond=0)
    for m in MINUTOS_DE_ACORDAR:
        alvo = base.replace(minute=m)
        if alvo > agora:
            return alvo
    return (base + timedelta(hours=1)).replace(minute=MINUTOS_DE_ACORDAR[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", required=True)
    ap.add_argument("--laco", type=int, default=0, help="minutos de laco (0 = roda uma vez)")
    ap.add_argument("--push", action="store_true", help="git add/commit/push apos cada publicacao")
    ap.add_argument("--esperar-quadro", action="store_true", help="(modo uma vez) espera o quadro da hora")
    args = ap.parse_args()

    email, senha, app_senha = (os.environ.get(k, "") for k in ("ZEUS_EMAIL", "ZEUS_SENHA", "APP_SENHA"))
    aberto = os.environ.get("CLIMA_ABERTO", "") == "1"
    if not email or not senha:
        print("Faltam ZEUS_EMAIL / ZEUS_SENHA no ambiente (segredos do GitHub).")
        sys.exit(1)
    if not aberto and not app_senha:
        print("Falta APP_SENHA (ou defina CLIMA_ABERTO=1 para publicar sem senha).")
        sys.exit(1)
    log("modo: " + ("ABERTO, sem senha" if aberto else "cifrado com a senha da pagina"))

    robo = Robo(args.saida, email, senha, app_senha, args.push, aberto)
    if args.laco <= 0:
        robo.rodar_uma_vez(args.esperar_quadro)
        return

    fim = datetime.now(timezone.utc) + timedelta(minutes=args.laco)
    log(f"laco ate {fim:%H:%M} UTC; publica em :{', :'.join(f'{m:02d}' for m in MINUTOS_DE_ACORDAR)}")
    robo.rodar_uma_vez(esperar=datetime.now(timezone.utc).minute < 25)  # primeira, ja
    while True:
        agora = datetime.now(timezone.utc)
        alvo = proximo_despertar(agora)
        if alvo >= fim:
            log("fim do laco; a proxima execucao agendada assume")
            break
        espera = (alvo - agora).total_seconds()
        log(f"proxima publicacao {alvo:%H:%M} UTC (em {espera / 60:.1f} min)")
        time.sleep(max(1, espera))
        robo.rodar_uma_vez(esperar=alvo.minute < 25)


if __name__ == "__main__":
    main()
