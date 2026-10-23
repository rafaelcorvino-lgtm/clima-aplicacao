#!/usr/bin/env python3
"""Historico por fazenda: cada quadro horario da Zeus (media e faixa das
estacoes da fazenda) guardado ao lado da previsao da area da fazenda
(Open-Meteo, no centro das estacoes) para a mesma hora.

Serve para responder, com numero, "quanto a previsao erra nesta fazenda?":
o app roda de 10 em 10 min, a Zeus fecha um quadro por hora, e entre um
quadro e outro a previsao e tudo o que se tem. SQLite num arquivo so
(historico.db), igual ao servidor dos pivos.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

TZ_BR = timezone(timedelta(hours=-3))
VARS = ("temp", "umid", "vento")


class Historico:
    def __init__(self, caminho: str):
        self._lock = threading.Lock()
        self._con = sqlite3.connect(caminho, check_same_thread=False)
        self._con.execute("""
            CREATE TABLE IF NOT EXISTS fazenda_quadros (
                fazenda    TEXT NOT NULL,
                hora_utc   TEXT NOT NULL,          -- fim do quadro Zeus, ISO em UTC
                prev_temp  REAL, prev_umid REAL, prev_vento REAL, prev_rajada REAL,
                temp_med   REAL, temp_min REAL, temp_max REAL, n_temp INTEGER,
                umid_med   REAL, umid_min REAL, umid_max REAL, n_umid INTEGER,
                vento_med  REAL, vento_min REAL, vento_max REAL, n_vento INTEGER,
                gravado_em TEXT,
                PRIMARY KEY (fazenda, hora_utc)
            )""")
        self._con.commit()

    def gravar(self, fazenda: str, hora_utc: str, previsao: dict | None, estacoes: dict):
        """estacoes = {temp: {med,min,max,n}, umid: {...}, vento: {...}} (pode faltar chave)."""
        if not fazenda or not hora_utc:
            return
        p = previsao or {}
        e = {k: estacoes.get(k) or {} for k in VARS}
        with self._lock:
            self._con.execute("""
                INSERT OR IGNORE INTO fazenda_quadros VALUES
                (?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?, ?)""", (
                fazenda, hora_utc,
                p.get("temperatura"), p.get("umidade"), p.get("vento"), p.get("rajada"),
                e["temp"].get("med"), e["temp"].get("min"), e["temp"].get("max"), e["temp"].get("n"),
                e["umid"].get("med"), e["umid"].get("min"), e["umid"].get("max"), e["umid"].get("n"),
                e["vento"].get("med"), e["vento"].get("min"), e["vento"].get("max"), e["vento"].get("n"),
                datetime.now(timezone.utc).isoformat()))
            self._con.commit()

    def resumo(self, fazenda: str, horas: int = 24) -> dict | None:
        """Erro medio absoluto, vies (previsao - media das estacoes) e pior caso."""
        desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
        with self._lock:
            linhas = self._con.execute("""
                SELECT prev_temp, temp_med, prev_umid, umid_med, prev_vento, vento_med
                FROM fazenda_quadros WHERE fazenda = ? AND hora_utc >= ?""", (fazenda, desde)).fetchall()
        if not linhas:
            return None
        saida = {"quadros": len(linhas)}
        for nome, a, b in (("temperatura", 0, 1), ("umidade", 2, 3), ("vento", 4, 5)):
            pares = [(l[a], l[b]) for l in linhas if l[a] is not None and l[b] is not None]
            if not pares:
                saida[nome] = None
                continue
            difs = [p - e for p, e in pares]
            saida[nome] = {
                "n": len(pares),
                "erro_medio": round(sum(abs(d) for d in difs) / len(difs), 1),
                "vies": round(sum(difs) / len(difs), 1),
                "maior": round(max(abs(d) for d in difs), 1),
            }
        return saida

    def serie(self, fazenda: str, horas: int = 48) -> list[dict]:
        desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
        with self._lock:
            linhas = self._con.execute("""
                SELECT hora_utc, prev_temp, prev_umid, prev_vento, prev_rajada,
                       temp_med, temp_min, temp_max, n_temp,
                       umid_med, umid_min, umid_max, n_umid,
                       vento_med, vento_min, vento_max, n_vento
                FROM fazenda_quadros WHERE fazenda = ? AND hora_utc >= ? ORDER BY hora_utc""",
                (fazenda, desde)).fetchall()
        saida = []
        for l in linhas:
            try:
                local = datetime.fromisoformat(l[0]).astimezone(TZ_BR).strftime("%d/%m %H:%M")
            except ValueError:
                local = l[0]
            saida.append({
                "hora": local,
                "previsao": {"temperatura": l[1], "umidade": l[2], "vento": l[3], "rajada": l[4]},
                "estacoes": {"temperatura": {"med": l[5], "min": l[6], "max": l[7], "n": l[8]},
                             "umidade": {"med": l[9], "min": l[10], "max": l[11], "n": l[12]},
                             "vento": {"med": l[13], "min": l[14], "max": l[15], "n": l[16]}},
            })
        return saida
