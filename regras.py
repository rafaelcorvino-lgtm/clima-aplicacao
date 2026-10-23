#!/usr/bin/env python3
"""Regras de condicao para aplicacao aerea.

Criterio do piloto (Rafael), que coincide com a leitura que a promotoria e a
IN MAPA 02/2008 fazem para aviacao agricola:

    temperatura   ate 30 C
    vento         entre 3 e 15 km/h
    umidade       acima de 50 %

Cada variavel recebe um dos tres estados:

    bom      dentro do criterio, com folga
    atencao  dentro do criterio, mas perto do limite (margem configuravel)
    ruim     fora do criterio

O estado geral e o pior dos tres. Delta T (diferenca entre bulbo seco e bulbo
umido, que mede evaporacao da gota) e um indicador extra: por padrao so
rebaixa para "atencao", nunca sozinho para "ruim". Rajada acima do limite
rebaixa para "ruim".
"""

from __future__ import annotations

import math

LIMITES_PADRAO = {
    "temperatura_max": 30.0,
    "vento_min": 3.0,
    "vento_max": 15.0,
    "umidade_min": 50.0,
    "rajada_max": 20.0,        # rajada acima disso = atencao (ruim se rajada_reprova=True; None desliga)
    "rajada_reprova": False,
    "delta_t_atencao": 8.0,    # faixa ideal 2..8; acima vira atencao
    "delta_t_ruim": 10.0,      # acima disso: ruim se delta_t_reprova=True
    "delta_t_reprova": False,
    "margem_temperatura": 2.0,  # 28..30 = atencao
    "margem_vento": 1.5,        # 3..4,5 e 13,5..15 = atencao
    "margem_umidade": 5.0,      # 50..55 = atencao
    "idade_max_min": 90,        # leitura mais velha que isso = sem dado confiavel (Zeus fecha quadro por hora)
}

ORDEM = {"bom": 0, "atencao": 1, "ruim": 2, "sem_dado": 3}
ROTULO = {"bom": "BOM PARA APLICAR", "atencao": "ATENÇÃO", "ruim": "RUIM, NÃO APLICAR",
          "sem_dado": "SEM DADO"}


def delta_t(temp_c: float | None, ur: float | None) -> float | None:
    """Delta T = T bulbo seco - T bulbo umido. Bulbo umido por Stull (2011)."""
    if temp_c is None or ur is None:
        return None
    ur = max(1.0, min(100.0, ur))
    t = temp_c
    tw = (t * math.atan(0.151977 * math.sqrt(ur + 8.313659))
          + math.atan(t + ur) - math.atan(ur - 1.676331)
          + 0.00391838 * ur ** 1.5 * math.atan(0.023101 * ur) - 4.686035)
    return round(t - tw, 1)


def _pior(*estados: str) -> str:
    return max(estados, key=lambda e: ORDEM[e])


def avaliar(leitura: dict, limites: dict | None = None) -> dict:
    """Recebe {temperatura, umidade, vento, rajada} e devolve a avaliacao.

    Devolve {status, rotulo, itens:[{nome, valor, unidade, status, motivo}], delta_t}.
    """
    L = dict(LIMITES_PADRAO)
    if limites:
        L.update({k: v for k, v in limites.items() if v is not None or k in L})
    itens = []

    t = leitura.get("temperatura")
    if t is None:
        itens.append(_item("Temperatura", None, "°C", "sem_dado", "sem leitura"))
    elif t > L["temperatura_max"]:
        itens.append(_item("Temperatura", t, "°C", "ruim", f"acima de {_f(L['temperatura_max'])} °C"))
    elif t > L["temperatura_max"] - L["margem_temperatura"]:
        itens.append(_item("Temperatura", t, "°C", "atencao", f"perto do limite de {_f(L['temperatura_max'])} °C"))
    else:
        itens.append(_item("Temperatura", t, "°C", "bom", f"até {_f(L['temperatura_max'])} °C"))

    v = leitura.get("vento")
    if v is None:
        itens.append(_item("Vento", None, "km/h", "sem_dado", "sem leitura"))
    elif v < L["vento_min"]:
        itens.append(_item("Vento", v, "km/h", "ruim", f"abaixo de {_f(L['vento_min'])} km/h (risco de inversão térmica)"))
    elif v > L["vento_max"]:
        itens.append(_item("Vento", v, "km/h", "ruim", f"acima de {_f(L['vento_max'])} km/h (deriva)"))
    elif v < L["vento_min"] + L["margem_vento"] or v > L["vento_max"] - L["margem_vento"]:
        itens.append(_item("Vento", v, "km/h", "atencao", f"perto do limite ({_f(L['vento_min'])} a {_f(L['vento_max'])} km/h)"))
    else:
        itens.append(_item("Vento", v, "km/h", "bom", f"entre {_f(L['vento_min'])} e {_f(L['vento_max'])} km/h"))

    u = leitura.get("umidade")
    if u is None:
        itens.append(_item("Umidade", None, "%", "sem_dado", "sem leitura"))
    elif u < L["umidade_min"]:
        itens.append(_item("Umidade", u, "%", "ruim", f"abaixo de {_f(L['umidade_min'])} %"))
    elif u < L["umidade_min"] + L["margem_umidade"]:
        itens.append(_item("Umidade", u, "%", "atencao", f"perto do mínimo de {_f(L['umidade_min'])} %"))
    else:
        itens.append(_item("Umidade", u, "%", "bom", f"acima de {_f(L['umidade_min'])} %"))

    r = leitura.get("rajada")
    if r is not None and L.get("rajada_max") is not None:
        if r > L["rajada_max"]:
            est = "ruim" if L.get("rajada_reprova") else "atencao"
            itens.append(_item("Rajada", r, "km/h", est, f"acima de {_f(L['rajada_max'])} km/h"))
        elif r > L["vento_max"]:
            itens.append(_item("Rajada", r, "km/h", "atencao", f"acima de {_f(L['vento_max'])} km/h"))
        else:
            itens.append(_item("Rajada", r, "km/h", "bom", "dentro do limite"))

    dt = delta_t(t, u)
    if dt is not None:
        if dt > L["delta_t_ruim"]:
            est = "ruim" if L.get("delta_t_reprova") else "atencao"
            itens.append(_item("Delta T", dt, "°C", est, f"acima de {_f(L['delta_t_ruim'])}: gota evapora rápido"))
        elif dt > L["delta_t_atencao"]:
            itens.append(_item("Delta T", dt, "°C", "atencao", f"acima de {_f(L['delta_t_atencao'])}: evaporação alta"))
        elif dt < 2:
            itens.append(_item("Delta T", dt, "°C", "atencao", "abaixo de 2: gota não seca, risco de escorrimento"))
        else:
            itens.append(_item("Delta T", dt, "°C", "bom", "faixa ideal de 2 a 8"))

    principais = [i["status"] for i in itens if i["nome"] in ("Temperatura", "Vento", "Umidade")]
    if all(s == "sem_dado" for s in principais):
        status = "sem_dado"
    else:
        status = _pior(*[s for s in (i["status"] for i in itens) if s != "sem_dado"] or ["sem_dado"])
        if "sem_dado" in principais and status == "bom":
            status = "atencao"  # faltando uma das tres, nao da pra dizer que esta bom
    return {"status": status, "rotulo": ROTULO[status], "itens": itens, "delta_t": dt}


def _item(nome, valor, unidade, status, motivo):
    return {"nome": nome, "valor": valor, "unidade": unidade, "status": status, "motivo": motivo}


def _f(x: float) -> str:
    return f"{x:g}".replace(".", ",")


if __name__ == "__main__":
    casos = [
        {"temperatura": 24, "umidade": 70, "vento": 8},
        {"temperatura": 29.5, "umidade": 52, "vento": 14},
        {"temperatura": 33, "umidade": 40, "vento": 2},
        {"temperatura": 26, "umidade": 60, "vento": 9, "rajada": 25},
        {"temperatura": None, "umidade": None, "vento": None},
    ]
    for c in casos:
        a = avaliar(c)
        print(c, "->", a["rotulo"], "| dT =", a["delta_t"])
        for i in a["itens"]:
            print("   ", i["nome"], i["valor"], i["status"], "-", i["motivo"])
