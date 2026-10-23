#!/usr/bin/env python3
"""Previsao horaria por fazenda, via Open-Meteo (api.open-meteo.com).

Serve para duas coisas:
  1. mostrar as proximas horas e apontar a proxima janela boa de aplicacao;
  2. cobrir fazenda que nao tem estacao Zeus (ai a fonte e marcada como
     "previsao", nunca confundida com leitura de estacao).

Open-Meteo e um servico aberto, sem chave e sem cadastro, que reempacota os
modelos nacionais (GFS, ECMWF, etc.). Vai so latitude e longitude na chamada.
So biblioteca padrao do Python.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

TZ_BR = timezone(timedelta(hours=-3))
URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT = 20


def buscar(lat: float, lng: float, horas: int = 24, horas_passadas: int = 6) -> dict:
    """Devolve {atual, horas (daqui pra frente), todas (inclui as ultimas horas_passadas)}.

    Cada hora: {hora, temperatura, umidade, vento, rajada, direcao, chuva_prob}.
    'todas' serve para comparar com o quadro horario da Zeus da mesma hora.
    """
    q = {
        "latitude": f"{lat:.5f}",
        "longitude": f"{lng:.5f}",
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m,precipitation",
        "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_gusts_10m,wind_direction_10m,precipitation_probability",
        "wind_speed_unit": "kmh",
        "timezone": "America/Sao_Paulo",
        "forecast_days": "2",
        "past_hours": str(int(horas_passadas)),
    }
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(q))
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        d = json.loads(r.read().decode())

    c = d.get("current") or {}
    atual = {
        "temperatura": c.get("temperature_2m"),
        "umidade": c.get("relative_humidity_2m"),
        "vento": c.get("wind_speed_10m"),
        "rajada": c.get("wind_gusts_10m"),
        "direcao": c.get("wind_direction_10m"),
        "hora": c.get("time"),
    }

    h = d.get("hourly") or {}
    tempos = h.get("time") or []
    agora = datetime.now(TZ_BR).replace(minute=0, second=0, microsecond=0)
    todas, saida = [], []
    for i, t in enumerate(tempos):
        try:
            quando = datetime.fromisoformat(t).replace(tzinfo=TZ_BR)
        except ValueError:
            continue
        item = {
            "hora": t,
            "temperatura": _idx(h.get("temperature_2m"), i),
            "umidade": _idx(h.get("relative_humidity_2m"), i),
            "vento": _idx(h.get("wind_speed_10m"), i),
            "rajada": _idx(h.get("wind_gusts_10m"), i),
            "direcao": _idx(h.get("wind_direction_10m"), i),
            "chuva_prob": _idx(h.get("precipitation_probability"), i),
        }
        todas.append(item)
        if quando >= agora and len(saida) < horas:
            saida.append(item)
    return {"atual": atual, "horas": saida, "todas": todas, "modelo": d.get("generationtime_ms") is not None}


def _idx(lista, i):
    try:
        return lista[i]
    except (TypeError, IndexError):
        return None


if __name__ == "__main__":
    import sys
    lat, lng = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) > 2 else (-16.349443, -47.547654)
    p = buscar(lat, lng, 12)
    print("agora:", p["atual"])
    for x in p["horas"]:
        print(x["hora"], x["temperatura"], "C", x["umidade"], "%", x["vento"], "km/h raj", x["rajada"])
