#!/usr/bin/env python3
"""Montagem do estado: estacoes Zeus + previsao + regras -> um dicionario.

Usado por dois lugares, para os dois nunca discordarem:
  - servidor.py (no notebook)
  - firebase/clima/main.py (na nuvem, a cada 10 min)
"""

from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timedelta, timezone

import previsao
import regras
import zeus

TZ_BR = timezone(timedelta(hours=-3))

# Clusters da Zeus que nao sao fazenda (grupos genericos)
CLUSTERS_GENERICOS = ("todas as pics", "pics ", "pivo ")
NIVEL_ZEUS = {"GREEN": "bom", "YELLOW": "atencao", "RED": "ruim"}


def distancia_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _dt(hora_iso: str | None) -> datetime | None:
    if not hora_iso:
        return None
    try:
        d = datetime.fromisoformat(hora_iso.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except ValueError:
        return None


def idade_min(hora_iso: str | None) -> float | None:
    d = _dt(hora_iso)
    return None if d is None else (datetime.now(timezone.utc) - d).total_seconds() / 60.0


def hora_local(hora_iso: str | None) -> str | None:
    d = _dt(hora_iso)
    return hora_iso if d is None and hora_iso else (d.astimezone(TZ_BR).strftime("%H:%M") if d else None)


def proxima_janela(horas: list[dict]) -> dict | None:
    """Primeira sequencia de horas 'bom' (ou 'atencao' se nao houver 'bom')."""
    for aceita in (("bom",), ("bom", "atencao")):
        ini = None
        for i, h in enumerate(horas):
            if h["status"] in aceita:
                if ini is None:
                    ini = i
            elif ini is not None:
                return {"inicio": horas[ini]["hora_local"], "fim": horas[i - 1]["hora_local"],
                        "horas": i - ini, "nivel": aceita[-1], "agora": ini == 0}
        if ini is not None:
            return {"inicio": horas[ini]["hora_local"], "fim": horas[-1]["hora_local"],
                    "horas": len(horas) - ini, "nivel": aceita[-1], "agora": ini == 0}
    return None


def estacoes_simuladas(cfg: dict) -> list[dict]:
    rnd = random.Random(int(time.time() // 300))
    saida = []
    for i, f in enumerate(cfg["fazendas"][:4]):
        for j in range(3):
            t = rnd.uniform(22, 34)
            u = rnd.uniform(35, 80)
            v = rnd.uniform(1, 18)
            tem_vento = j == 0
            saida.append({
                "id": 1000 + i * 10 + j, "nome": ("Sede " if j == 0 else f"Pv {j + 10} ") + f["nome"].replace("Faz. ", ""),
                "clusters": [f["nome"]], "lat": f["lat"] + 0.01 * j, "lng": f["lng"] - 0.01 * j,
                "tipo": "FULL_MONITORING" if tem_vento else "MONITORING", "tem_vento": tem_vento,
                "comunicacao": "OK", "desativada": False, "quadro": "TIME_FRAME_60M",
                "ultimo_envio": datetime.now(timezone.utc).isoformat(),
                "leitura": {"temperatura": round(t, 1), "umidade": round(u), "vento": round(v, 1) if tem_vento else None,
                            "vento_inst": round(v * 0.8, 1) if tem_vento else None,
                            "rajada": round(v * 1.4, 1) if tem_vento else None, "direcao": rnd.randint(0, 359) if tem_vento else None,
                            "chuva_hora": 0.0, "pressao": None,
                            "hora": (datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)).isoformat()},
            })
    return saida


def buscar_estacoes(cliente: zeus.ClienteZeus | None, cfg: dict, simular: bool) -> tuple[list[dict], dict, dict, list[str]]:
    """Devolve (estacoes com leitura, parecer_zeus, previsao_zeus, avisos)."""
    if simular:
        return estacoes_simuladas(cfg), {}, {}, ["modo simulado: dados inventados"]
    if not cliente:
        return [], {}, {}, ["sem credenciais Zeus (preencha segredos.txt)"]
    avisos = []
    try:
        ests = cliente.estacoes()
    except Exception as e:  # noqa: BLE001
        return [], {}, {}, [f"Zeus: {e}"]
    parecer, prev_zeus = {}, {}
    try:
        parecer = cliente.parecer_pulverizacao()
        prev_zeus = cliente.previsao_pulverizacao()
    except Exception as e:  # noqa: BLE001
        avisos.append(f"parecer Zeus: {e}")
    for e in ests:
        e["leitura"] = None
        if e["desativada"] or e["id"] is None:
            continue
        try:
            e["leitura"] = cliente.ultima_leitura(e["id"], e["tem_vento"])
        except Exception as ex:  # noqa: BLE001
            avisos.append(f"{e['nome']}: {ex}")
    return ests, parecer, prev_zeus, avisos


class CachePrevisao:
    """Guarda a previsao por fazenda por 30 min (Open-Meteo nao muda mais rapido que isso)."""

    def __init__(self):
        self._c: dict[str, tuple[float, dict]] = {}

    def buscar(self, chave: str, lat: float, lng: float, horas: int) -> dict | None:
        agora = time.time()
        c = self._c.get(chave)
        if c and agora - c[0] < 1800:
            return c[1]
        try:
            p = previsao.buscar(lat, lng, horas)
        except Exception as e:  # noqa: BLE001
            return {"erro": str(e)} if not c else c[1]
        self._c[chave] = (agora, p)
        return p


def internet_na_hora(p: dict | None, hora_iso_utc: str | None) -> dict | None:
    """Pega, na previsao Open-Meteo, a hora igual ao fim do quadro Zeus (hora local)."""
    d = _dt(hora_iso_utc)
    if not p or p.get("erro") or d is None:
        return None
    alvo = d.astimezone(TZ_BR).strftime("%Y-%m-%dT%H:00")
    for h in p.get("todas") or []:
        if h.get("hora") == alvo:
            return {"temperatura": h.get("temperatura"), "umidade": h.get("umidade"),
                    "vento": h.get("vento"), "rajada": h.get("rajada"), "hora_local": alvo[11:16]}
    return None


def _dif(a, b):
    return None if a is None or b is None else round(b - a, 1)


def _faixa(valores: list) -> dict | None:
    """Media, minimo, maximo e quantidade, ignorando None."""
    v = [x for x in valores if x is not None]
    if not v:
        return None
    return {"med": round(sum(v) / len(v), 1), "min": round(min(v), 1), "max": round(max(v), 1), "n": len(v)}


def montar(cfg: dict, cliente: zeus.ClienteZeus | None, simular: bool = False,
           cache: CachePrevisao | None = None, historico=None) -> dict:
    """Monta o estado completo (o JSON que a pagina consome).

    historico (opcional): objeto com gravar(estacao, fazenda, internet) e
    resumo(ids, horas); guarda cada quadro Zeus ao lado da internet.
    """
    cache = cache or CachePrevisao()
    L = cfg.get("limites", {})
    horas_prev = int(cfg.get("horas_de_previsao", 12))
    raio = float(cfg.get("raio_km", 40))
    ests, parecer, prev_zeus, avisos = buscar_estacoes(cliente, cfg, simular)

    fazendas = [{"nome": f["nome"], "lat": f["lat"], "lng": f["lng"], "estacoes": [], "origem": "config"}
                for f in cfg.get("fazendas", [])]

    def fazenda_de(e: dict) -> dict:
        # 1) pela posicao: fazenda do config mais perto, dentro do raio
        if e.get("lat") is not None and e.get("lng") is not None:
            melhor, dist = None, None
            for f in fazendas:
                if f.get("lat") is None:
                    continue
                d = distancia_km(e["lat"], e["lng"], f["lat"], f["lng"])
                if dist is None or d < dist:
                    melhor, dist = f, d
            if melhor is not None and dist <= raio:
                return melhor
        # 2) pelo cluster da Zeus (pulando grupos genericos)
        nomes = [c for c in e.get("clusters") or [] if c and not c.lower().startswith(CLUSTERS_GENERICOS)]
        nomes.sort(key=lambda c: 0 if c.lower().startswith("fazenda") else 1)
        nome = nomes[0] if nomes else (e.get("local") or "Sem fazenda")
        for f in fazendas:
            if f["nome"].lower() == nome.lower():
                return f
        nova = {"nome": nome, "lat": e.get("lat"), "lng": e.get("lng"), "estacoes": [], "origem": "zeus"}
        fazendas.append(nova)
        return nova

    agora_janela = datetime.now(timezone.utc) - timedelta(hours=3)
    for e in ests:
        if e.get("desativada"):
            continue
        f = fazenda_de(e)
        lei = e.get("leitura") or {}
        idade = idade_min(lei.get("hora"))
        aval = regras.avaliar(lei, L)
        velha = idade is not None and idade > float(L.get("idade_max_min", 90))
        if velha:
            aval["status"] = "sem_dado"
            aval["rotulo"] = "LEITURA ANTIGA"
        par = parecer.get(str(e.get("id")))
        jan = []
        for w in prev_zeus.get(str(e.get("id")), []):
            d = _dt(w.get("hora"))
            if d and d >= agora_janela:
                ini = d.astimezone(TZ_BR)
                fim = ini + timedelta(hours=3)
                jan.append({"hora_local": f"{ini:%H}h–{fim:%H}h", "status": NIVEL_ZEUS.get(w.get("nivel"), "sem_dado"),
                            "causas": w.get("causas", [])})
        f["estacoes"].append({
            "id": e.get("id"), "nome": e.get("nome"), "tipo": e.get("tipo"),
            "tem_vento": e.get("tem_vento"), "comunicacao": e.get("comunicacao"),
            "lat": e.get("lat"), "lng": e.get("lng"),
            "leitura": {k: v for k, v in lei.items() if k != "_bruto"},
            "hora_local": hora_local(lei.get("hora")),
            "idade_min": None if idade is None else round(idade),
            "leitura_antiga": velha,
            "avaliacao": aval,
            "parecer_zeus": None if not par else {"status": NIVEL_ZEUS.get(par.get("nivel"), "sem_dado"),
                                                  "nivel": par.get("nivel"), "causas": par.get("causas", []),
                                                  "hora_local": hora_local(par.get("hora"))},
            "previsao_zeus": jan[:8],
        })

    for f in fazendas:
        # ordem: estacoes com vento primeiro, depois por nome
        f["estacoes"].sort(key=lambda s: (0 if s["tem_vento"] else 1, s["nome"].lower()))
        # Estacao de vento sem termo-higrometro (ex.: Sao Jorge): completa temperatura e
        # umidade com a estacao vizinha mais recente da mesma fazenda, e diz de onde veio.
        doadoras = [s for s in f["estacoes"]
                    if not s["leitura_antiga"] and s["leitura"].get("temperatura") is not None
                    and s["leitura"].get("umidade") is not None]
        doadoras.sort(key=lambda s: (s["idade_min"] if s["idade_min"] is not None else 9e9))
        for s in f["estacoes"]:
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

        # A previsao da fazenda e feita no CENTRO das estacoes dela (a area de fato),
        # nao no ponto do mapa nem numa estacao so.
        pontos = [(s["lat"], s["lng"]) for s in f["estacoes"] if s.get("lat") is not None and s.get("lng") is not None]
        if pontos:
            f["lat"] = round(sum(p[0] for p in pontos) / len(pontos), 5)
            f["lng"] = round(sum(p[1] for p in pontos) / len(pontos), 5)
            f["centro_de"] = len(pontos)

        # referencia = a PIOR estacao com sensor de vento e leitura valida (a fazenda e
        # grande; se um ponto esta ruim, o piloto precisa ver). Empate: a mais recente.
        # Sem nenhuma com vento, a pior so de temperatura/umidade (vira no maximo "atencao").
        validas = [s for s in f["estacoes"] if s["avaliacao"]["status"] != "sem_dado"]
        com_vento = [s for s in validas if s["tem_vento"]]
        grupo = com_vento or validas
        if grupo:
            grupo.sort(key=lambda s: (-regras.ORDEM[s["avaliacao"]["status"]],
                                      s["idade_min"] if s["idade_min"] is not None else 9e9))
            ref = grupo[0]
            f["status"] = ref["avaliacao"]["status"]
            f["rotulo"] = ref["avaliacao"]["rotulo"]
            f["fonte"] = "estacao"
            f["referencia"] = ref["nome"]
            f["referencia_id"] = ref["id"]
            f["sem_vento"] = not ref["tem_vento"]
        else:
            f["status"] = "sem_dado"
            f["rotulo"] = "SEM ESTAÇÃO" if not f["estacoes"] else "SEM LEITURA RECENTE"
            f["fonte"] = "nenhuma"
            f["referencia"] = None
            f["referencia_id"] = None
            f["sem_vento"] = False

        f["previsao"] = None
        f["proxima_janela"] = None
        f["comparacao"] = None
        f["comparacao_24h"] = None
        if f.get("lat") is not None and f.get("lng") is not None:
            p = cache.buscar(f["nome"], f["lat"], f["lng"], horas_prev)
            if p and not p.get("erro"):
                # Previsao da area x estacoes da fazenda, no ultimo quadro Zeus fechado:
                # media e faixa das estacoes contra o valor previsto para a mesma hora.
                frescas = [s for s in f["estacoes"] if not s["leitura_antiga"] and s["leitura"].get("hora")]
                if frescas:
                    hora = max(s["leitura"]["hora"] for s in frescas)
                    grupo = [s for s in frescas if s["leitura"]["hora"] == hora]
                    proprias = [s for s in grupo if not s["leitura"].get("emprestado_de")]
                    est = {
                        "temp": _faixa([s["leitura"].get("temperatura") for s in proprias]),
                        "umid": _faixa([s["leitura"].get("umidade") for s in proprias]),
                        "vento": _faixa([s["leitura"].get("vento") for s in grupo if s["tem_vento"]]),
                    }
                    prev = internet_na_hora(p, hora)
                    f["comparacao"] = {
                        "hora_local": hora_local(hora),
                        "previsao": prev,
                        "estacoes": {"temperatura": est["temp"], "umidade": est["umid"], "vento": est["vento"]},
                        "dif": {
                            "temperatura": _dif((est["temp"] or {}).get("med"), (prev or {}).get("temperatura")),
                            "umidade": _dif((est["umid"] or {}).get("med"), (prev or {}).get("umidade")),
                            "vento": _dif((est["vento"] or {}).get("med"), (prev or {}).get("vento")),
                        },
                    }
                    if historico is not None and not simular:
                        try:
                            historico.gravar(f["nome"], hora, prev, est)
                            f["comparacao_24h"] = historico.resumo(f["nome"], 24)
                        except Exception as e:  # noqa: BLE001
                            avisos.append(f"histórico {f['nome']}: {e}")
                # Rajada de modelo numerico sai sempre alta demais; na previsao ela
                # e mostrada, mas nao reprova. Na estacao, reprova (ate "atencao").
                horas = []
                for h in p["horas"]:
                    a = regras.avaliar({k: v for k, v in h.items() if k != "rajada"}, L)
                    horas.append({**h, "status": a["status"], "delta_t": a["delta_t"],
                                  "hora_local": h["hora"][11:16] if h.get("hora") else None})
                atual = p.get("atual") or {}
                aval_prev = regras.avaliar({k: v for k, v in atual.items() if k != "rajada"}, L)
                f["previsao"] = {"atual": {**atual, "status": aval_prev["status"], "delta_t": aval_prev["delta_t"]},
                                 "horas": horas, "fonte": "Open-Meteo"}
                f["proxima_janela"] = proxima_janela(horas)
                if f["fonte"] == "nenhuma" and atual.get("temperatura") is not None:
                    f["status"] = aval_prev["status"]
                    f["rotulo"] = aval_prev["rotulo"] + " (previsão)"
                    f["fonte"] = "previsao"
            elif p and p.get("erro"):
                avisos.append(f"previsão {f['nome']}: {p['erro']}")

    ordem = {"ruim": 0, "atencao": 1, "bom": 2, "sem_dado": 3}
    fazendas.sort(key=lambda f: (0 if f["estacoes"] else 1, f["nome"]))
    return {
        "atualizado_em": datetime.now(TZ_BR).isoformat(),
        "limites": L,
        "fazendas": fazendas,
        "avisos": avisos,
        "simulado": simular,
        "total_estacoes": sum(len(f["estacoes"]) for f in fazendas),
        "resumo": {k: sum(1 for f in fazendas if f["status"] == k) for k in ordem},
    }


if __name__ == "__main__":
    import os
    aqui = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(aqui, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    print(json.dumps(montar(cfg, None, simular=True)["resumo"], ensure_ascii=False))
