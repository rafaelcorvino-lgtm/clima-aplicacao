#!/usr/bin/env python3
"""Robô Zeus do MariAgro (mora neste repositório, o clima-aplicacao).

Lê as estações meteorológicas DIRETO da API da Zeus (a mesma que o portal
portal.zeusagro.com usa) e grava o resultado no banco do MariAgro, em
    grupos/<grupo>/zeus
no Firebase Realtime Database. O app MariAgro lê dali (o navegador não consegue
chamar a API da Zeus: ela só aceita chamadas vindas do próprio portal).

Por que aqui e não no repositório mariagro: o mariagro é privado, e robô em
repositório privado gasta a cota de 2.000 minutos/mês do GitHub Actions (um
laço de 24 h/dia acabou com ela em 2 dias, 09 a 11/11/2026). Este repositório
é público, com minutos ilimitados, e já tem o login da Zeus nos segredos.
Usa o zeus.py, o regras.py e o config.json daqui. Não depende do estado.json
nem do resto do Clima de Aplicação: é um robô à parte (workflow próprio,
.github/workflows/mariagro-zeus.yml).

O que vai para o banco, por estação:
  - leitura do último quadro horário fechado (temperatura, umidade, vento,
    rajada, direção, chuva na hora, pressão, radiação);
  - avaliação bom/atenção/ruim por variável (regras.py, critério do Rafael);
  - parecer e previsão de pulverização da própria Zeus;
  - CHUVA ACUMULADA: agora (hora em curso), hoje, ontem, 7 dias, 30 dias e
    desde o início do ano agrícola (config.json: safra_inicio), mais o
    volume de cada um dos últimos 30 dias.
E, por fazenda do MariAgro (as áreas cadastradas no próprio app dizem onde
cada fazenda fica): estação de referência, status e média/máximo da chuva.

Uso:
  python nuvem/mariagro_zeus.py --uma-vez               roda uma vez e grava
  python nuvem/mariagro_zeus.py --laco 350              fica 350 min gravando a cada 10 min
  python nuvem/mariagro_zeus.py --uma-vez --seco        monta tudo e NÃO grava (teste)
  python nuvem/mariagro_zeus.py --grupo cc-logtest      grava num grupo de teste
  python nuvem/mariagro_zeus.py --segredos caminho.txt  arquivo ZEUS_EMAIL=/ZEUS_SENHA=
Login: variáveis ZEUS_EMAIL e ZEUS_SENHA (ou --segredos). Só biblioteca padrão.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)   # zeus.py, regras.py e config.json ficam na raiz do repositório
sys.path.insert(0, RAIZ)
import regras  # noqa: E402
import zeus  # noqa: E402

RTDB = "https://calendario-aerea-26140-default-rtdb.firebaseio.com"
TZ_BR = timezone(timedelta(hours=-3))
NIVEL_ZEUS = {"GREEN": "bom", "YELLOW": "atencao", "RED": "ruim"}
DIAS_GUARDADOS = 30
SECO = False


def log(msg: str):
    print(f"[{datetime.now(TZ_BR):%d/%m %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# utilidades
# ---------------------------------------------------------------------------

def carregar_config() -> dict:
    with open(os.path.join(RAIZ, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def http_json(url: str, metodo: str = "GET", corpo=None, timeout: int = 60):
    dados = None if corpo is None else json.dumps(corpo, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=dados, method=metodo)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8")
        return json.loads(txt) if txt else None


def distancia_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _dt(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def hora_local(iso) -> str | None:
    d = _dt(iso)
    return f"{d.astimezone(TZ_BR):%H:%M}" if d else None


def idade_min(iso) -> float | None:
    d = _dt(iso)
    return (datetime.now(timezone.utc) - d).total_seconds() / 60 if d else None


def chave_fb(nome: str) -> str:
    """Chave válida no Firebase (sem . # $ [ ] /)."""
    return re.sub(r"[.#$\[\]/]", "_", nome).strip() or "sem_nome"


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _r1(v):
    return None if v is None else round(v, 1)


# ---------------------------------------------------------------------------
# fazendas do MariAgro (pelas áreas cadastradas no app)
# ---------------------------------------------------------------------------

def _pontos(area: dict) -> list[tuple[float, float]]:
    if isinstance(area.get("centre"), list) and len(area["centre"]) == 2:
        return [(float(area["centre"][0]), float(area["centre"][1]))]
    saida = []

    def anda(x):
        if isinstance(x, list):
            if len(x) == 2 and all(isinstance(n, (int, float)) for n in x):
                saida.append((float(x[0]), float(x[1])))
            else:
                for y in x:
                    anda(y)
        elif isinstance(x, dict) and "lat" in x and "lng" in x:
            saida.append((float(x["lat"]), float(x["lng"])))

    anda(area.get("coords") or area.get("latlngs") or [])
    if not saida:
        return []
    # centro do polígono (média dos vértices) basta para medir distância até a estação
    return [(sum(p[0] for p in saida) / len(saida), sum(p[1] for p in saida) / len(saida))]


def ler_fazendas(grupo: str, cfg: dict) -> dict:
    """{nome: {pontos:[(lat,lng)], lat, lng, areas:n, origem}} a partir das áreas do app."""
    fazendas: dict = {}
    try:
        areas = http_json(f"{RTDB}/grupos/{grupo}/areas.json") or {}
    except Exception as e:  # noqa: BLE001
        log(f"AVISO: não li as áreas do grupo {grupo}: {e}")
        areas = {}
    for a in areas.values():
        if not isinstance(a, dict):
            continue
        nome = (((a.get("props") or {}).get("ident") or {}).get("fazenda") or "").strip()
        if not nome:
            continue
        f = fazendas.setdefault(nome, {"nome": nome, "pontos": [], "areas": 0, "origem": "areas do app"})
        f["pontos"].extend(_pontos(a))
        f["areas"] += 1
    # fazendas do config (ponto central) completam as que ainda não têm área cadastrada
    for fc in cfg.get("fazendas", []):
        if fc.get("nome") and fc["nome"] not in fazendas and fc.get("lat") is not None:
            fazendas[fc["nome"]] = {"nome": fc["nome"], "pontos": [(fc["lat"], fc["lng"])], "areas": 0, "origem": "config"}
    for f in fazendas.values():
        if f["pontos"]:
            f["lat"] = round(sum(p[0] for p in f["pontos"]) / len(f["pontos"]), 5)
            f["lng"] = round(sum(p[1] for p in f["pontos"]) / len(f["pontos"]), 5)
        else:
            f["lat"] = f["lng"] = None
    return fazendas


def fazenda_da_estacao(est: dict, fazendas: dict, raio_km: float):
    if est.get("lat") is None or est.get("lng") is None:
        return None, None
    melhor, dist = None, None
    for f in fazendas.values():
        for p in f["pontos"]:
            d = distancia_km(est["lat"], est["lng"], p[0], p[1])
            if dist is None or d < dist:
                melhor, dist = f["nome"], d
    if melhor is not None and dist <= raio_km:
        return melhor, round(dist, 1)
    return None, (None if dist is None else round(dist, 1))


# ---------------------------------------------------------------------------
# Zeus
# ---------------------------------------------------------------------------

def _q(**k) -> str:
    return "?" + urllib.parse.urlencode(k, doseq=True)


def relatorio(cli, ids: list, inicio: str, fim: str, unit: str) -> dict:
    """/reports/pic-data por estação: {picId: [buckets]} (unit = HOURS | DAYS | TOTAL)."""
    saida = {}
    for i in range(0, len(ids), 60):   # lotes, por garantia (46 estações cabem num só)
        lote = ids[i:i + 60]
        r = cli._get("/reports/pic-data/gmt-3" + _q(ids=lote, start=inicio, end=fim, unit=unit, reportViewEnum="PICS"))
        for p in (r if isinstance(r, list) else []):
            if isinstance(p, dict) and p.get("picId") is not None:
                saida[str(p["picId"])] = [d for d in (p.get("data") or []) if isinstance(d, dict)]
    return saida


def leitura_do_bucket(b: dict, tem_vento: bool) -> dict:
    vento = rajada = vento_inst = direcao = None
    if tem_vento:
        vento = _num(b.get("windSpeedAverage"))
        vento_inst = _num(b.get("windSpeedInst"))
        rajada = _num(b.get("sustainedGustSpeed"))
        direcao = _num(b.get("windDirectionAverage") if b.get("windDirectionAverage") is not None else b.get("windDirectionInst"))
    return {
        "temperatura": _num(b.get("temperatureInst")),
        "temperatura_max": _num(b.get("temperatureMax")),
        "umidade": _num(b.get("humidityInst")),
        "umidade_min": _num(b.get("humidityMin")),
        "vento": vento, "vento_inst": vento_inst, "rajada": rajada, "direcao": direcao,
        "chuva_hora": _num(b.get("rain")),
        "pressao": _num(b.get("atmosphericPressureInst")),
        "radiacao": _num(b.get("solarIrradianceInst")),
        "hora": b.get("finished"),
        "hora_inicio": b.get("started"),
    }


def montar(cli, cfg: dict, grupo: str) -> dict:
    L = cfg.get("limites", {})
    raio = float(cfg.get("raio_km", 40))
    avisos: list[str] = []
    agora = datetime.now(TZ_BR)
    hoje = agora.date()
    ontem = hoje - timedelta(days=1)
    safra_md = str(cfg.get("safra_inicio", "09-01"))
    safra_ini = datetime.strptime(f"{hoje.year}-{safra_md}", "%Y-%m-%d").date()
    if safra_ini > hoje:
        safra_ini = safra_ini.replace(year=hoje.year - 1)
    d30_ini = hoje - timedelta(days=DIAS_GUARDADOS - 1)

    fazendas = ler_fazendas(grupo, cfg)
    log(f"fazendas do app: {len(fazendas)} ({', '.join(sorted(fazendas))})")

    ests = [e for e in cli.estacoes() if not e.get("desativada") and e.get("id") is not None]
    ids = [e["id"] for e in ests]
    log(f"estações ativas na Zeus: {len(ests)}")

    horas = relatorio(cli, ids, hoje.isoformat(), hoje.isoformat(), "HOURS")
    dias = relatorio(cli, ids, d30_ini.isoformat(), hoje.isoformat(), "DAYS")
    # ano agrícola: um total só (a API soma do início ao fim, incluindo hoje)
    safra = relatorio(cli, ids, safra_ini.isoformat(), hoje.isoformat(), "TOTAL")
    try:
        rn = cli._get("/pics/rain_now")
        chuva_agora = {str(x.get("id")): _num(x.get("rain")) for x in (rn if isinstance(rn, list) else []) if isinstance(x, dict)}
    except Exception as e:  # noqa: BLE001
        avisos.append(f"rain_now: {e}")
        chuva_agora = {}
    parecer = cli.parecer_pulverizacao()
    prev_zeus = cli.previsao_pulverizacao()

    estacoes: dict = {}
    for e in ests:
        sid = str(e["id"])
        hb = horas.get(sid) or []
        if hb:
            lei = leitura_do_bucket(hb[-1], bool(e.get("tem_vento")))
        else:
            try:   # sem quadro hoje: pede a última leitura direto (estação parada ou virada do dia)
                lei = {k: v for k, v in cli.ultima_leitura(e["id"], bool(e.get("tem_vento"))).items() if k != "_bruto"}
            except Exception as ex:  # noqa: BLE001
                avisos.append(f"{e['nome']}: sem leitura ({ex})")
                lei = {}
        idade = idade_min(lei.get("hora"))
        velha = idade is not None and idade > float(L.get("idade_max_min", 90))
        aval = regras.avaliar(lei, L)
        if velha:
            aval["status"] = "sem_dado"
            aval["rotulo"] = "LEITURA ANTIGA"

        # chuva
        por_dia = {}
        for b in dias.get(sid) or []:
            d = _dt(b.get("started"))
            if d and b.get("rain") is not None:
                por_dia[d.astimezone(TZ_BR).date().isoformat()] = _r1(_num(b["rain"]))
        def soma(desde):
            vals = [v for k, v in por_dia.items() if k >= desde.isoformat() and v is not None]
            return _r1(sum(vals)) if vals else None
        tot = (safra.get(sid) or [{}])[0] if safra.get(sid) else {}
        chuva = {
            "agora": chuva_agora.get(sid),
            "hoje": por_dia.get(hoje.isoformat()),
            "ontem": por_dia.get(ontem.isoformat()),
            "d7": soma(hoje - timedelta(days=6)),
            "d30": soma(d30_ini),
            "safra": _r1(_num(tot.get("rain"))) if tot else None,
            "safra_desde": safra_ini.isoformat(),
            "dias": por_dia,
            "dias_com_dado": len(por_dia),
        }
        faz, dist = fazenda_da_estacao(e, fazendas, raio)
        par = parecer.get(sid)
        jan = []
        corte = datetime.now(timezone.utc) - timedelta(hours=3)
        for w in prev_zeus.get(sid, []):
            d = _dt(w.get("hora"))
            if d and d >= corte:
                ini = d.astimezone(TZ_BR)
                fim = ini + timedelta(hours=3)
                jan.append({"hora_local": f"{ini:%H}h–{fim:%H}h", "status": NIVEL_ZEUS.get(w.get("nivel"), "sem_dado"),
                            "causas": w.get("causas", [])})
        estacoes[sid] = {
            "id": e["id"], "nome": e["nome"], "tipo": e.get("tipo"),
            "tem_vento": bool(e.get("tem_vento")), "comunicacao": e.get("comunicacao"),
            "lat": e.get("lat"), "lng": e.get("lng"),
            "fazenda": faz, "dist_km": dist,
            "leitura": lei,
            "hora_local": hora_local(lei.get("hora")),
            "idade_min": None if idade is None else round(idade),
            "leitura_antiga": velha,
            "avaliacao": aval,
            "parecer_zeus": None if not par else {"status": NIVEL_ZEUS.get(par.get("nivel"), "sem_dado"),
                                                  "nivel": par.get("nivel"), "causas": par.get("causas", []),
                                                  "hora_local": hora_local(par.get("hora"))},
            "previsao_zeus": jan[:8],
            "chuva": chuva,
        }

    # por fazenda: empresta temperatura/umidade para estação só de vento, referência e chuva
    saida_faz: dict = {}
    grupos: dict = {}
    for s in estacoes.values():
        grupos.setdefault(s["fazenda"] or "", []).append(s)
    for nome, lista in grupos.items():
        lista.sort(key=lambda s: (0 if s["tem_vento"] else 1, s["nome"].lower()))
        doadoras = [s for s in lista if not s["leitura_antiga"] and s["leitura"].get("temperatura") is not None
                    and s["leitura"].get("umidade") is not None]
        doadoras.sort(key=lambda s: (s["idade_min"] if s["idade_min"] is not None else 9e9))
        for s in lista:
            lei = s["leitura"]
            if not s["tem_vento"] or s["leitura_antiga"] or not doadoras:
                continue
            if lei.get("temperatura") is not None and lei.get("umidade") is not None:
                continue
            d = doadoras[0]
            if d is s:
                continue
            lei["temperatura"] = d["leitura"]["temperatura"]
            lei["umidade"] = d["leitura"]["umidade"]
            lei["emprestado_de"] = d["nome"]
            s["avaliacao"] = regras.avaliar(lei, L)
        if not nome:
            continue
        validas = [s for s in lista if s["avaliacao"]["status"] != "sem_dado"]
        com_vento = [s for s in validas if s["tem_vento"]]
        g = com_vento or validas
        f = {"nome": nome, "estacoes": [s["id"] for s in lista], "n_estacoes": len(lista),
             "origem": (fazendas.get(nome) or {}).get("origem")}
        if g:
            g.sort(key=lambda s: (-regras.ORDEM[s["avaliacao"]["status"]], s["idade_min"] if s["idade_min"] is not None else 9e9))
            ref = g[0]
            f.update({"status": ref["avaliacao"]["status"], "rotulo": ref["avaliacao"]["rotulo"],
                      "referencia": ref["nome"], "referencia_id": ref["id"], "sem_vento": not ref["tem_vento"]})
        else:
            f.update({"status": "sem_dado", "rotulo": "SEM LEITURA RECENTE", "referencia": None, "referencia_id": None})

        def agreg(campo):
            vals = [s["chuva"].get(campo) for s in lista if s["chuva"].get(campo) is not None]
            if not vals:
                return None
            mx = max(vals)
            nome_max = next(s["nome"] for s in lista if s["chuva"].get(campo) == mx)
            return {"media": _r1(sum(vals) / len(vals)), "max": _r1(mx), "max_em": nome_max, "min": _r1(min(vals)), "n": len(vals)}
        f["chuva"] = {c: agreg(c) for c in ("agora", "hoje", "ontem", "d7", "d30", "safra")}
        f["chuva"]["safra_desde"] = safra_ini.isoformat()
        saida_faz[chave_fb(nome)] = f

    fora = [s["nome"] for s in estacoes.values() if not s["fazenda"]]
    if fora:
        avisos.append("estações fora de todas as fazendas (raio %.0f km): %s" % (raio, ", ".join(fora)))
    return {
        "atualizado_em": agora.isoformat(timespec="seconds"),
        "fonte": "API Zeus (api.zeusagro.com), lida direto pelo robô do MariAgro (clima-aplicacao/nuvem/mariagro_zeus.py)",
        "limites": L,
        "raio_km": raio,
        "safra_desde": safra_ini.isoformat(),
        "total_estacoes": len(estacoes),
        "estacoes": estacoes,
        "fazendas": saida_faz,
        "avisos": avisos,
    }


# ---------------------------------------------------------------------------
# principal
# ---------------------------------------------------------------------------

def gravar(grupo: str, estado: dict) -> None:
    http_json(f"{RTDB}/grupos/{grupo}/zeus.json", "PUT", estado, timeout=90)


def uma_rodada(cli, cfg, grupo, ultimo_hash: str | None, salvar_em: str | None) -> str | None:
    estado = montar(cli, cfg, grupo)
    corpo = dict(estado)
    corpo.pop("atualizado_em", None)
    h = hashlib.sha1(json.dumps(corpo, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if salvar_em:
        with open(salvar_em, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, indent=1)
    if h == ultimo_hash:
        log("nada mudou desde a última gravação")
        return h
    if SECO:
        log(f"(seco) não gravei: {estado['total_estacoes']} estações, {len(estado['fazendas'])} fazendas; avisos: {estado['avisos']}")
        return h
    gravar(grupo, estado)
    nf = len(estado["fazendas"])
    log(f"gravado em grupos/{grupo}/zeus: {estado['total_estacoes']} estações, {nf} fazendas; "
        f"{len(json.dumps(estado))} bytes" + (f"; avisos: {' | '.join(estado['avisos'])}" if estado["avisos"] else ""))
    return h


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grupo", default="corvino-fazendas")
    ap.add_argument("--laco", type=int, default=0, help="minutos vivo, gravando a cada --intervalo")
    ap.add_argument("--intervalo", type=int, default=10, help="minutos entre rodadas (padrão 10)")
    ap.add_argument("--uma-vez", action="store_true")
    ap.add_argument("--segredos", default=os.path.join(RAIZ, "segredos.txt"))
    ap.add_argument("--salvar", default=None, help="também salva o JSON num arquivo (conferência)")
    ap.add_argument("--seco", action="store_true", help="monta tudo mas NÃO grava no Firebase (teste)")
    args = ap.parse_args()
    global SECO
    SECO = args.seco

    cfg = carregar_config()
    seg = zeus.carregar_segredos(args.segredos)
    email = os.environ.get("ZEUS_EMAIL") or seg.get("ZEUS_EMAIL")
    senha = os.environ.get("ZEUS_SENHA") or seg.get("ZEUS_SENHA")
    if not email or not senha:
        print("Falta o login da Zeus: ZEUS_EMAIL e ZEUS_SENHA (variáveis de ambiente ou --segredos).")
        sys.exit(2)
    cli = zeus.ClienteZeus(email, senha, os.path.join(RAIZ, ".sessao-zeus.json"))

    h = None
    fim = time.time() + args.laco * 60
    while True:
        try:
            h = uma_rodada(cli, cfg, args.grupo, h, args.salvar)
        except Exception as e:  # noqa: BLE001
            log(f"ERRO na rodada: {e!r}")
        if args.uma_vez or args.laco <= 0 or time.time() + args.intervalo * 60 > fim:
            break
        time.sleep(args.intervalo * 60)


if __name__ == "__main__":
    main()
