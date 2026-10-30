#!/usr/bin/env python3
"""Clima de Aplicacao: esta bom ou ruim para aplicar agora, em cada fazenda.

Junta a leitura das estacoes Zeus (portal.zeusagro.com) com a previsao das
proximas horas e aplica os limites do piloto (config.json). Serve uma pagina
para o celular e um JSON. A montagem do estado fica em estado.py, que a
funcao na nuvem (firebase/clima) tambem usa.

    GET /               pagina
    GET /api/estado     JSON com tudo
    GET /api/atualizar  forca uma atualizacao agora
    GET /satelite/x.png imagens Sentinel-2 por fazenda (satelite.py)
    GET /health         sonda simples

Uso:
    python servidor.py --porta 8090
    python servidor.py --simular        (dados inventados, sem login na Zeus)

Credenciais em segredos.txt (veja segredos_exemplo.txt). So biblioteca
padrao do Python, como o servidor dos pivos. A senha da pagina (APP_SENHA)
pode vir por cookie (formulario /login) ou pelo cabecalho X-Senha (o mesmo
que a pagina publicada usa).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

import estado as mod_estado  # noqa: E402
import historico as mod_historico  # noqa: E402
import satelite as mod_satelite  # noqa: E402
import zeus  # noqa: E402

TZ_BR = mod_estado.TZ_BR


def carregar_config() -> dict:
    with open(os.path.join(AQUI, "config.json"), encoding="utf-8") as f:
        return json.load(f)


class Estado:
    def __init__(self, cliente: zeus.ClienteZeus | None, simular: bool):
        self.cliente = cliente
        self.simular = simular
        self.dados = {"atualizado_em": None, "fazendas": [], "avisos": []}
        self._lock = threading.Lock()
        self._cache = mod_estado.CachePrevisao()
        self.historico = mod_historico.Historico(os.path.join(AQUI, "historico.db"))
        cfg_sat = carregar_config().get("satelite", {})
        self.satelite = mod_satelite.Satelite(carregar_config(), a_cada_h=float(cfg_sat.get("a_cada_h", 3)))
        self.satelite_ativo = bool(cfg_sat.get("ativo", True))

    def snapshot(self) -> dict:
        with self._lock:
            d = json.loads(json.dumps(self.dados))
        # O satelite anda em fio proprio e no seu proprio ritmo; entra no estado so na saida.
        mod_satelite.aplicar(d, self.satelite.dados())
        return d

    def atualizar(self):
        novo = mod_estado.montar(carregar_config(), self.cliente, self.simular, self._cache, self.historico)
        with self._lock:
            self.dados = novo


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Servidor(ThreadingHTTPServer):
    daemon_threads = True
    # No Windows, "reuse address" deixa DOIS servidores abrirem a mesma porta, e o
    # antigo (talvez sem senha) continua respondendo. Melhor falhar com "porta em uso".
    allow_reuse_address = os.name != "nt"


def _html() -> bytes:
    with open(os.path.join(AQUI, "pagina.html"), "rb") as f:
        return f.read()


class Handler(BaseHTTPRequestHandler):
    estado: Estado = None  # type: ignore
    senha: str = ""
    chave_cookie: bytes = b""

    def log_message(self, fmt, *args):  # silencia o log por requisicao
        if os.environ.get("APP_DEBUG"):
            super().log_message(fmt, *args)

    # --- sessao simples ---------------------------------------------------
    def _token(self) -> str:
        return hmac.new(self.chave_cookie, self.senha.encode(), hashlib.sha256).hexdigest()

    def _logado(self) -> bool:
        if not self.senha:
            return True
        cab = self.headers.get("X-Senha")
        if cab is not None:
            return hmac.compare_digest(cab, self.senha)
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "sessao" in c and hmac.compare_digest(c["sessao"].value, self._token())

    def _responder(self, corpo: bytes, tipo="text/html; charset=utf-8", status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corpo)

    def _pagina_login(self, erro=""):
        html = f"""<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Clima de Aplicação</title>
<body style="font-family:system-ui;background:#111;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh;margin:0">
<form method="post" action="/login" style="background:#222;padding:24px;border-radius:12px;width:280px">
<h2 style="margin-top:0">Clima de Aplicação</h2><p style="color:#f66">{erro}</p>
<input name="senha" type="password" placeholder="senha" autofocus style="width:100%;padding:12px;font-size:18px;box-sizing:border-box">
<button style="width:100%;padding:12px;font-size:18px;margin-top:12px">Entrar</button></form></body></html>"""
        self._responder(html.encode())

    # --- rotas --------------------------------------------------------------
    def do_GET(self):
        caminho = self.path.split("?")[0]
        if caminho == "/health":
            d = self.estado.snapshot()
            return self._responder(json.dumps({"ok": True, "atualizado_em": d.get("atualizado_em")}).encode(),
                                   "application/json")
        if caminho == "/login":
            return self._pagina_login()
        if caminho == "/sair":
            return self._responder(b"", status=302, extra={"Location": "/login",
                                                          "Set-Cookie": "sessao=; Max-Age=0; Path=/"})
        if not self._logado():
            if caminho.startswith("/api/"):
                return self._responder(b'{"erro":"sem sessao"}', "application/json", 401)
            return self._responder(b"", status=302, extra={"Location": "/login"})
        if caminho == "/":
            return self._responder(_html())
        if caminho.startswith("/satelite/"):
            # imagens Sentinel-2 geradas por satelite.py (so nomes simples, nada de caminho)
            nome = os.path.basename(caminho)
            arq = os.path.join(self.estado.satelite.pasta, nome)
            if not re.fullmatch(r"[a-z0-9-]+\.png", nome) or not os.path.exists(arq):
                return self._responder(b"nao encontrado", "text/plain", 404)
            with open(arq, "rb") as f:
                return self._responder(f.read(), "image/png", extra={"Cache-Control": "max-age=600"})
        if caminho == "/api/estado":
            return self._responder(json.dumps(self.estado.snapshot(), ensure_ascii=False).encode(),
                                   "application/json; charset=utf-8")
        if caminho == "/api/historico":
            q = parse_qs(self.path.partition("?")[2])
            fazenda = (q.get("fazenda") or [""])[0]
            try:
                horas = int((q.get("horas") or ["48"])[0])
            except ValueError:
                return self._responder(b'{"erro":"parametro invalido"}', "application/json", 400)
            serie = self.estado.historico.serie(fazenda, horas)
            return self._responder(json.dumps({"fazenda": fazenda, "quadros": serie}, ensure_ascii=False).encode(),
                                   "application/json; charset=utf-8")
        if caminho == "/api/atualizar":
            try:
                self.estado.atualizar()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self._responder(json.dumps({"erro": str(e)}).encode(), "application/json", 500)
            return self._responder(json.dumps(self.estado.snapshot(), ensure_ascii=False).encode(),
                                   "application/json; charset=utf-8")
        self._responder(b"nao encontrado", "text/plain", 404)

    def do_POST(self):
        if self.path.split("?")[0] != "/login":
            return self._responder(b"nao encontrado", "text/plain", 404)
        n = int(self.headers.get("Content-Length", "0") or 0)
        corpo = parse_qs(self.rfile.read(n).decode())
        senha = (corpo.get("senha") or [""])[0]
        if self.senha and hmac.compare_digest(senha, self.senha):
            seguro = "; Secure" if os.environ.get("APP_HTTPS") == "1" else ""
            return self._responder(b"", status=302, extra={
                "Location": "/", "Set-Cookie": f"sessao={self._token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000{seguro}"})
        time.sleep(1)
        self._pagina_login("senha errada")


def laco_atualizacao(estado: Estado, minutos: float):
    while True:
        try:
            estado.atualizar()
            d = estado.snapshot()
            print(f"[{datetime.now(TZ_BR):%H:%M}] atualizado: {d['total_estacoes']} estacoes, {d['resumo']}  avisos={len(d['avisos'])}", flush=True)
            for a in d["avisos"][:5]:
                print("   aviso:", a, flush=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(max(60, minutos * 60))


def laco_satelite(estado: Estado):
    """Sentinel-2 em segundo plano: a primeira analise leva minutos (baixa alguns MB
    por fazenda); depois so refaz quando o catalogo tem cena nova, a cada a_cada_h."""
    while True:
        try:
            if estado.satelite.precisa():
                d = estado.satelite.atualizar()
                print(f"[{datetime.now(TZ_BR):%H:%M}] satelite: {len(d.get('fazendas', {}))} fazendas analisadas", flush=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        time.sleep(600)


def main():
    ap = argparse.ArgumentParser(description="Clima de Aplicacao")
    ap.add_argument("--porta", type=int, default=8090)
    ap.add_argument("--simular", action="store_true", help="dados inventados, sem Zeus")
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    cfg = carregar_config()
    seg = zeus.carregar_segredos(os.path.join(AQUI, "segredos.txt"))
    cliente = None
    if not args.simular and seg.get("ZEUS_EMAIL") and seg.get("ZEUS_SENHA"):
        cliente = zeus.ClienteZeus(seg["ZEUS_EMAIL"], seg["ZEUS_SENHA"],
                                   os.path.join(AQUI, ".sessao-zeus.json"))
    elif not args.simular:
        print("AVISO: sem ZEUS_EMAIL/ZEUS_SENHA em segredos.txt; so previsao. Use --simular para testar.")

    estado = Estado(cliente, args.simular)
    Handler.estado = estado
    # No modo simulado (dados inventados) a pagina nao pede senha: e so para testar a tela.
    Handler.senha = "" if args.simular else (seg.get("APP_SENHA", "") or os.environ.get("APP_SENHA", ""))
    Handler.chave_cookie = os.urandom(32)

    threading.Thread(target=laco_atualizacao, args=(estado, float(cfg.get("atualizar_a_cada_min", 5))),
                     daemon=True).start()
    if estado.satelite_ativo:
        threading.Thread(target=laco_satelite, args=(estado,), daemon=True).start()
    try:
        srv = Servidor((args.host, args.porta), Handler)
    except OSError as e:
        print(f"Nao consegui abrir a porta {args.porta}: {e}")
        print("Ja existe um Clima de Aplicacao rodando? Feche a outra janela e tente de novo.")
        sys.exit(2)
    print(f"Clima de Aplicacao em http://localhost:{args.porta}  (Ctrl+C para parar)"
          + ("  [SIMULADO]" if args.simular else "")
          + ("  [com senha na pagina]" if Handler.senha else "  [SEM senha na pagina]"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
