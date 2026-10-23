#!/usr/bin/env python3
"""Liga o Clima de Aplicacao e o deixa acessivel no celular pelo Tailscale.

    python aplicacao-aerea/iniciar.py

O que ele faz:
  1. confere segredos.txt (login Zeus e APP_SENHA; sem APP_SENHA nao expoe);
  2. se o Tailscale estiver instalado e logado, liga o "tailscale serve", que
     publica a porta 8090 em https://<este-pc>.<sua-rede>.ts.net com certificado
     valido, so para os aparelhos da sua rede Tailscale;
  3. sobe o servidor (APP_HTTPS=1 quando ha Tailscale, para o cookie ser Secure);
  4. imprime o endereco para abrir no celular.

Sem Tailscale, sobe mesmo assim e mostra o endereco da rede local (Wi-Fi).
Ctrl+C derruba o servidor; o "tailscale serve" fica ligado (e inofensivo:
sem servidor atras, da erro 502). Para desligar: tailscale serve reset
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
PORTA = 8090
CANDIDATOS = [
    shutil.which("tailscale"),
    r"C:\Program Files\Tailscale\tailscale.exe",
    r"C:\Program Files (x86)\Tailscale\tailscale.exe",
]


def tailscale_exe() -> str | None:
    for c in CANDIDATOS:
        if c and os.path.exists(c):
            return c
    return None


def ts(exe: str, *args: str, timeout=20) -> tuple[int, str]:
    p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ip_local() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    sys.path.insert(0, AQUI)
    import zeus
    seg = zeus.carregar_segredos(os.path.join(AQUI, "segredos.txt"))
    if not seg.get("ZEUS_EMAIL") or not seg.get("ZEUS_SENHA"):
        print("Falta ZEUS_EMAIL/ZEUS_SENHA em segredos.txt. Abra o arquivo e preencha.")
        sys.exit(1)
    senha_pagina = seg.get("APP_SENHA", "")

    env = dict(os.environ)
    endereco_ts = None
    exe = tailscale_exe()
    if not exe:
        print("Tailscale nao encontrado. Instale em https://tailscale.com/download e entre na conta;")
        print("depois rode este iniciar.py de novo. Por enquanto, so na rede local.")
    else:
        rc, saida = ts(exe, "status", "--json")
        estado = {}
        try:
            estado = json.loads(saida) if rc == 0 else {}
        except json.JSONDecodeError:
            pass
        backend = estado.get("BackendState")
        if backend != "Running":
            print(f"Tailscale instalado, mas nao conectado (estado: {backend or 'desconhecido'}).")
            print("Abra o Tailscale na bandeja do Windows e entre na conta; depois rode de novo.")
        elif not senha_pagina:
            print("Tailscale conectado, mas APP_SENHA esta vazio em segredos.txt. Sem senha nao exponho a pagina.")
        else:
            dns = (estado.get("Self") or {}).get("DNSName", "").rstrip(".")
            rc, saida = ts(exe, "serve", "--bg", f"--https=443", f"http://127.0.0.1:{PORTA}")
            if rc != 0:
                # versoes antigas do CLI
                rc, saida = ts(exe, "serve", "--bg", str(PORTA))
            if rc == 0 and dns:
                endereco_ts = f"https://{dns}/"
                env["APP_HTTPS"] = "1"
            else:
                print("Nao consegui ligar o tailscale serve:", saida.strip()[:400])
                ip4 = ts(exe, "ip", "-4")[1].strip().splitlines()
                if ip4:
                    endereco_ts = f"http://{ip4[0]}:{PORTA}/  (sem HTTPS)"

    print()
    print("=" * 64)
    print("  CLIMA DE APLICACAO")
    if endereco_ts:
        print(f"  No celular (com Tailscale ligado):  {endereco_ts}")
    print(f"  Na rede local (Wi-Fi da casa):      http://{ip_local()}:{PORTA}/")
    print(f"  Neste PC:                           http://localhost:{PORTA}/")
    print("  Senha da pagina: a de APP_SENHA em segredos.txt" if senha_pagina else "  Pagina SEM senha (APP_SENHA vazio)")
    print("=" * 64)
    print()

    servidor = os.path.join(AQUI, "servidor.py")
    try:
        subprocess.call([sys.executable, servidor, "--porta", str(PORTA)], env=env, cwd=AQUI)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
