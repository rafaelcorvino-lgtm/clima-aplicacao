#!/usr/bin/env python3
"""Satelite: ultima imagem Sentinel-2 limpa de cada fazenda e NDVI por pivo.

O Google Earth mostra a foto de 2023. O Sentinel-2 (ESA) passa a cada 5 dias
com 10 m por pixel: nao se ve o pivo nitido como no Google, mas se ve o
circulo verde ou seco, falha de irrigacao e a evolucao da cultura. E o NDVI
((NIR - vermelho) / (NIR + vermelho)) resume o vigor em um numero por pivo.

De onde vem cada coisa (tudo aberto, sem chave e sem cadastro):
  - os PIVOS: do mapa MariAgro (Firebase, leitura publica) -- centro e raio
    dos circulos desenhados por fazenda. So aparece aqui o que foi desenhado
    la como "pivo".
  - as CENAS: catalogo STAC do Earth Search (Element 84) sobre o espelho
    Sentinel-2 L2A da AWS. Os arquivos sao GeoTIFF "cloud optimized":
    deflate em blocos de 1024 px. Com pedidos HTTP por faixa de bytes
    (Range) le-se so os blocos que cobrem a fazenda, nunca a cena inteira
    (cada cena tem 100 km x 100 km).
  - a NUVEM: a propria mascara da cena (SCL, 20 m). Um pivo so recebe NDVI
    de uma cena em que ele esta pelo menos 80 % limpo; senao a busca volta
    no tempo, pivo por pivo, ate 60 dias.

Saida (pasta satelite/, que o servidor serve e o robo publica):
  satelite.json          resultado por fazenda (ver analisar_fazenda)
  <fazenda>-foto.png     cor natural (TCI) da area dos pivos
  <fazenda>-ndvi.png     NDVI colorido da mesma area (nuvem em cinza)
  pivos.json             cache dos pivos do mapa

So biblioteca padrao do Python (como o resto do app): o TIFF e lido na mao,
o PNG e escrito na mao. Pura e simples e lento o bastante para rodar em
segundo plano, nunca na requisicao.

    python satelite.py                 analisa todas as fazendas
    python satelite.py --fazenda Divina
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import struct
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import zlib
from array import array
from datetime import datetime, timedelta, timezone
from itertools import accumulate

TZ_BR = timezone(timedelta(hours=-3))

STAC = "https://earth-search.aws.element84.com/v1/search"
COLECAO = "sentinel-2-l2a"
MAPA = "https://calendario-aerea-26140-default-rtdb.firebaseio.com/grupos/corvino-fazendas/areas.json"

DIAS_DE_BUSCA = 60        # ate onde voltar procurando cena limpa
MAX_CENAS = 40            # cenas examinadas por fazenda, no maximo
LIMPO_MIN = 0.80          # fracao do pivo sem nuvem para aceitar a cena
MARGEM_M = 400            # borda em volta dos pivos na imagem
MAX_PX = 900              # maior lado da imagem publicada
TIMEOUT = 60
TENTATIVAS = 3
PASTA_PADRAO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "satelite")

# SCL (Scene Classification Layer): 0 sem dado, 1 saturado, 2 sombra escura,
# 3 sombra de nuvem, 4 vegetacao, 5 solo, 6 agua, 7 nao classificado,
# 8/9 nuvem media/alta prob., 10 cirro, 11 neve.
SCL_LIMPO = frozenset((4, 5, 6))

# Faixas do NDVI (por pivo e na legenda da pagina).
CLASSES = (
    (0.60, "vigoroso", "#2e9e5b"),
    (0.40, "medio", "#8bc34a"),
    (0.20, "fraco", "#d9a21b"),
    (-1.0, "solo", "#b08968"),
)
COR_NUBLADO = "#8a8f99"


def log(msg: str):
    print(f"[satelite {datetime.now(TZ_BR):%H:%M:%S}] {msg}", flush=True)


def slug(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return re.sub(r"^faz-", "", s) or "fazenda"


_conexoes: dict[str, http.client.HTTPSConnection] = {}
_trava = threading.Lock()


def _http(url: str, dados: bytes | None = None, cabecalhos: dict | None = None) -> bytes:
    """GET/POST com conexao reaproveitada por servidor (sao centenas de pedidos
    Range pequenos: abrir TLS a cada um era o que mais custava) e ate 3
    tentativas em erro de rede ou 5xx; 4xx falha na hora."""
    u = urllib.parse.urlsplit(url)
    caminho = u.path + ("?" + u.query if u.query else "")
    cab = dict(cabecalhos or {})
    if dados is not None:
        cab["Content-Type"] = "application/json"
    erro: Exception = RuntimeError("sem tentativa")
    for t in range(TENTATIVAS):
        with _trava:
            con = _conexoes.get(u.netloc)
            if con is None:
                con = http.client.HTTPSConnection(u.netloc, timeout=TIMEOUT)
                _conexoes[u.netloc] = con
            try:
                con.request("POST" if dados is not None else "GET", caminho, body=dados, headers=cab)
                r = con.getresponse()
                corpo = r.read()
                if r.status < 400:
                    return corpo
                if r.status < 500:
                    raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, None)
                erro = urllib.error.HTTPError(url, r.status, r.reason, r.headers, None)
            except urllib.error.HTTPError:
                raise
            except (OSError, http.client.HTTPException) as e:
                erro = e
            con.close()
            _conexoes.pop(u.netloc, None)
        time.sleep(1 + 2 * t)
    raise erro


# ---------------------------------------------------------------------------
# Pivos do mapa MariAgro
# ---------------------------------------------------------------------------

def _distancia_km(lat1, lng1, lat2, lng2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _numero(nome: str) -> int:
    m = re.search(r"(\d+)", nome or "")
    return int(m.group(1)) if m else 10 ** 6


def pivos_do_mapa(cfg: dict, cache_arq: str | None = None, max_idade_h: float = 6) -> dict[str, list[dict]]:
    """{nome da fazenda (config.json): [{nome, lat, lng, raio}]}.

    Um pivo entra na fazenda cujo nome o MariAgro registrou; sem nome igual,
    na fazenda do config mais perto (dentro de raio_km). O resultado fica em
    cache (pivos.json) e vale por 6 h; se o mapa nao responder, usa o cache
    que houver, de qualquer idade.
    """
    if cache_arq and os.path.exists(cache_arq):
        idade_h = (time.time() - os.path.getmtime(cache_arq)) / 3600
        if idade_h < max_idade_h:
            with open(cache_arq, encoding="utf-8") as f:
                return json.load(f)
    try:
        areas = json.loads(_http(MAPA).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        if cache_arq and os.path.exists(cache_arq):
            log(f"mapa MariAgro fora do ar ({e}); usando pivos.json antigo")
            with open(cache_arq, encoding="utf-8") as f:
                return json.load(f)
        raise

    fazendas = cfg.get("fazendas", [])
    raio_km = float(cfg.get("raio_km", 40))
    por_nome = {f["nome"].lower(): f["nome"] for f in fazendas}
    saida: dict[str, list[dict]] = {}
    for a in (areas or {}).values():
        if not isinstance(a, dict) or a.get("type") != "pivot":
            continue
        c = a.get("centre") or []
        if len(c) != 2 or not a.get("radius"):
            continue
        lat, lng, raio = float(c[0]), float(c[1]), float(a["radius"])
        nome_faz = (((a.get("props") or {}).get("ident") or {}).get("fazenda") or "").strip()
        destino = por_nome.get(nome_faz.lower())
        if not destino and fazendas:
            perto = min(fazendas, key=lambda f: _distancia_km(lat, lng, f["lat"], f["lng"]))
            if _distancia_km(lat, lng, perto["lat"], perto["lng"]) <= raio_km:
                destino = perto["nome"]
        if not destino:
            continue
        saida.setdefault(destino, []).append({
            "nome": (a.get("name") or "?").strip(), "lat": lat, "lng": lng, "raio": raio,
        })
    for lista in saida.values():
        lista.sort(key=lambda p: (_numero(p["nome"]), p["nome"]))
    if cache_arq:
        os.makedirs(os.path.dirname(cache_arq), exist_ok=True)
        with open(cache_arq, "w", encoding="utf-8") as f:
            json.dump(saida, f, ensure_ascii=False, indent=1)
    return saida


# ---------------------------------------------------------------------------
# Projecao UTM (WGS 84), so o sentido lat/lng -> leste/norte
# ---------------------------------------------------------------------------

def para_utm(lat: float, lng: float, zona: int, sul: bool = True) -> tuple[float, float]:
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    k0 = 0.9996
    lng0 = math.radians((zona - 1) * 6 - 180 + 3)
    phi, lam = math.radians(lat), math.radians(lng)
    sp, cp, tp = math.sin(phi), math.cos(phi), math.tan(phi)
    n = a / math.sqrt(1 - e2 * sp * sp)
    t = tp * tp
    c = ep2 * cp * cp
    aa = cp * (lam - lng0)
    m = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
             - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * phi)
             + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * phi)
             - (35 * e2 ** 3 / 3072) * math.sin(6 * phi))
    leste = 500000 + k0 * n * (aa + (1 - t + c) * aa ** 3 / 6
                              + (5 - 18 * t + t * t + 72 * c - 58 * ep2) * aa ** 5 / 120)
    norte = k0 * (m + n * tp * (aa * aa / 2 + (5 - t + 9 * c + 4 * c * c) * aa ** 4 / 24
                                + (61 - 58 * t + t * t + 600 * c - 330 * ep2) * aa ** 6 / 720))
    if sul:
        norte += 10000000
    return leste, norte


# ---------------------------------------------------------------------------
# Leitor minimo de GeoTIFF "cloud optimized" (deflate, blocos, predictor 2)
# ---------------------------------------------------------------------------

class Cog:
    """Le janelas de um GeoTIFF remoto so com pedidos Range.

    Suporta exatamente o formato dos Sentinel-2 da AWS: little-endian,
    deflate (8), predictor horizontal (2), 8 ou 16 bits sem sinal, blocos
    quadrados, amostras intercaladas. Qualquer outra coisa levanta erro.
    """

    def __init__(self, url: str):
        self.url = url
        self._blocos: dict[int, array] = {}
        self._ler_cabecalho()

    def _ler_cabecalho(self):
        b = _http(self.url, cabecalhos={"Range": "bytes=0-65535"})
        if b[:2] != b"II" or struct.unpack("<H", b[2:4])[0] != 42:
            raise ValueError("TIFF inesperado (nao e little-endian classico)")
        off = struct.unpack("<I", b[4:8])[0]
        n = struct.unpack("<H", b[off:off + 2])[0]
        tags: dict[int, tuple] = {}
        tam = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 11: 4, 12: 8, 16: 8}
        fmt = {1: "B", 3: "H", 4: "I", 11: "f", 12: "d", 16: "Q"}
        for i in range(n):
            e = b[off + 2 + i * 12: off + 14 + i * 12]
            tag, tipo, cnt = struct.unpack("<HHI", e[:8])
            total = tam[tipo] * cnt
            dados = e[8:8 + total] if total <= 4 else b[struct.unpack("<I", e[8:12])[0]:][:total]
            if tipo in fmt and len(dados) == total:
                tags[tag] = struct.unpack("<%d%s" % (cnt, fmt[tipo]), dados)
            elif tipo == 2:
                tags[tag] = (dados.split(b"\0", 1)[0].decode("latin-1"),)
        self.largura, self.altura = tags[256][0], tags[257][0]
        self.bits = tags[258][0]
        self.amostras = tags.get(277, (1,))[0]
        if tags.get(259, (1,))[0] != 8 or tags.get(317, (1,))[0] not in (1, 2) or self.bits not in (8, 16):
            raise ValueError("compressao/predictor/bits fora do esperado em " + self.url)
        self.predictor = tags.get(317, (1,))[0]
        self.bloco = tags[322][0]
        if tags[323][0] != self.bloco:
            raise ValueError("bloco nao quadrado")
        self.offsets, self.tamanhos = tags[324], tags[325]
        self.escala = tags[33550][0]
        tie = tags[33922]
        self.e0, self.n0 = tie[3], tie[4]
        self.blocos_por_linha = -(-self.largura // self.bloco)
        self.tipo = "H" if self.bits == 16 else "B"

    # --- geo ---------------------------------------------------------------
    def pixel(self, leste: float, norte: float) -> tuple[float, float]:
        """(coluna, linha) fracionarias de uma coordenada UTM da mesma zona."""
        return (leste - self.e0) / self.escala, (self.n0 - norte) / self.escala

    # --- blocos --------------------------------------------------------------
    def _bloco(self, bx: int, by: int) -> array | None:
        k = by * self.blocos_por_linha + bx
        if k in self._blocos:
            return self._blocos[k]
        if not (0 <= bx < self.blocos_por_linha and 0 <= by < -(-self.altura // self.bloco)):
            return None
        ini, tam = self.offsets[k], self.tamanhos[k]
        if tam == 0:
            a = None  # bloco vazio = sem dado
        else:
            bruto = zlib.decompress(_http(self.url, cabecalhos={"Range": f"bytes={ini}-{ini + tam - 1}"}))
            a = array(self.tipo)
            a.frombytes(bruto)
            if self.predictor == 2:
                self._desfazer_predictor(a)
        self._blocos[k] = a
        return a

    def _desfazer_predictor(self, a: array):
        passo = self.bloco * self.amostras
        mascara = 0xFFFF if self.bits == 16 else 0xFF
        soma = (lambda x, y: (x + y) & mascara)
        for ini in range(0, len(a), passo):
            for c in range(self.amostras):
                fatia = a[ini + c: ini + passo: self.amostras]
                a[ini + c: ini + passo: self.amostras] = array(self.tipo, accumulate(fatia, soma))

    def ler(self, c0: int, l0: int, c1: int, l1: int) -> array:
        """Janela [c0,c1) x [l0,l1) em pixels, amostras intercaladas, 0 fora da imagem."""
        w, h, s = c1 - c0, l1 - l0, self.amostras
        saida = array(self.tipo, bytes(w * h * s * (2 if self.bits == 16 else 1)))
        bt = self.bloco
        for by in range(max(0, l0 // bt), min(-(-self.altura // bt), -(-l1 // bt))):
            for bx in range(max(0, c0 // bt), min(self.blocos_por_linha, -(-c1 // bt))):
                bl = self._bloco(bx, by)
                if bl is None:
                    continue
                ca, cb = max(c0, bx * bt), min(c1, (bx + 1) * bt)
                la, lb = max(l0, by * bt), min(l1, (by + 1) * bt)
                for lin in range(la, lb):
                    fonte = ((lin - by * bt) * bt + (ca - bx * bt)) * s
                    dest = ((lin - l0) * w + (ca - c0)) * s
                    saida[dest: dest + (cb - ca) * s] = bl[fonte: fonte + (cb - ca) * s]
        return saida


# ---------------------------------------------------------------------------
# Catalogo de cenas
# ---------------------------------------------------------------------------

def buscar_cenas(bbox: list[float], dias: int = DIAS_DE_BUSCA, limite: int = MAX_CENAS) -> list[dict]:
    """Cenas Sentinel-2 L2A que cobrem a caixa [oeste, sul, leste, norte], mais novas primeiro."""
    fim = datetime.now(timezone.utc)
    ini = fim - timedelta(days=dias)
    corpo = {
        "collections": [COLECAO],
        "bbox": bbox,
        "datetime": f"{ini:%Y-%m-%dT00:00:00Z}/{fim:%Y-%m-%dT23:59:59Z}",
        "limit": limite,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        "fields": {
            "include": ["id", "bbox", "properties.datetime", "properties.eo:cloud_cover", "properties.proj:epsg",
                        "properties.earthsearch:boa_offset_applied", "properties.s2:processing_baseline",
                        "assets.red.href", "assets.nir.href", "assets.visual.href", "assets.scl.href"],
            "exclude": ["geometry", "links"],
        },
    }
    d = json.loads(_http(STAC, json.dumps(corpo).encode()).decode("utf-8"))
    cenas = []
    for it in d.get("features", []):
        p, a = it.get("properties", {}), it.get("assets", {})
        epsg = p.get("proj:epsg")
        if not epsg or not all(k in a for k in ("red", "nir", "visual", "scl")):
            continue
        quando = datetime.fromisoformat(p["datetime"].replace("Z", "+00:00"))
        # Desde a baseline 04.00 (2022) os DN tem offset de -1000; o Earth Search
        # ja o aplica nos COGs (boa_offset_applied). Se nao aplicou, descontamos.
        baseline = p.get("s2:processing_baseline") or "00.00"
        offset = 0 if p.get("earthsearch:boa_offset_applied", True) or baseline < "04.00" else 1000
        cenas.append({
            "id": it["id"], "quando": quando, "data": quando.astimezone(TZ_BR).strftime("%Y-%m-%d"),
            "nuvem": p.get("eo:cloud_cover"), "zona": int(epsg) % 100, "sul": int(epsg) // 100 == 327,
            "bbox": it.get("bbox"), "offset": offset,
            "red": a["red"]["href"], "nir": a["nir"]["href"], "visual": a["visual"]["href"], "scl": a["scl"]["href"],
        })
    return cenas


def _dentro(cena: dict, lat: float, lng: float, folga: float = 0.01) -> bool:
    b = cena.get("bbox")
    return bool(b) and b[0] + folga <= lng <= b[2] - folga and b[1] + folga <= lat <= b[3] - folga


# ---------------------------------------------------------------------------
# NDVI
# ---------------------------------------------------------------------------

def classe(ndvi: float | None) -> tuple[str, str]:
    if ndvi is None:
        return "nublado", COR_NUBLADO
    for lim, nome, cor in CLASSES:
        if ndvi >= lim:
            return nome, cor
    return CLASSES[-1][1], CLASSES[-1][2]


def _cor_ndvi(v: float) -> tuple[int, int, int]:
    paradas = ((-1.0, (70, 70, 70)), (0.0, (166, 97, 26)), (0.2, (223, 194, 125)), (0.4, (199, 234, 70)),
               (0.6, (77, 175, 74)), (0.8, (0, 104, 55)), (1.0, (0, 60, 30)))
    for (x0, c0), (x1, c1) in zip(paradas, paradas[1:]):
        if v <= x1:
            t = 0 if x1 == x0 else (v - x0) / (x1 - x0)
            return tuple(int(round(c0[i] + (c1[i] - c0[i]) * t)) for i in range(3))
    return paradas[-1][1]


LUT_NDVI = [_cor_ndvi(-1 + i / 100.0) for i in range(201)]


def _pixels_do_pivo(cog: Cog, pivo: dict, zona: int, sul: bool, escala: float):
    """Gera (coluna, linha) dos pixels dentro do circulo, na grade de 'escala' m."""
    e, n = para_utm(pivo["lat"], pivo["lng"], zona, sul)
    cc, lc = cog.pixel(e, n)
    r = pivo["raio"] / escala
    r2 = r * r
    for lin in range(int(lc - r), int(lc + r) + 2):
        dy = lin + 0.5 - lc
        for col in range(int(cc - r), int(cc + r) + 2):
            dx = col + 0.5 - cc
            if dx * dx + dy * dy <= r2:
                yield col, lin


def _nuvem_no_pivo(scl: Cog, pivo: dict, cena: dict) -> tuple[float, bool]:
    """(fracao limpa, cobre?) usando a SCL de 20 m. cobre=False se ha pixel sem dado."""
    pts = list(_pixels_do_pivo(scl, pivo, cena["zona"], cena["sul"], 20))
    if not pts:
        return 0.0, False
    c0, c1 = min(p[0] for p in pts), max(p[0] for p in pts) + 1
    l0, l1 = min(p[1] for p in pts), max(p[1] for p in pts) + 1
    jan = scl.ler(c0, l0, c1, l1)
    w = c1 - c0
    limpos = total = 0
    for col, lin in pts:
        v = jan[(lin - l0) * w + (col - c0)]
        if v == 0:
            return 0.0, False
        total += 1
        limpos += v in SCL_LIMPO
    return limpos / total, True


def _ndvi_do_pivo(red: Cog, nir: Cog, scl: Cog, pivo: dict, cena: dict) -> dict:
    pts = list(_pixels_do_pivo(red, pivo, cena["zona"], cena["sul"], 10))
    c0, c1 = min(p[0] for p in pts), max(p[0] for p in pts) + 1
    l0, l1 = min(p[1] for p in pts), max(p[1] for p in pts) + 1
    w = c1 - c0
    jr, jn = red.ler(c0, l0, c1, l1), nir.ler(c0, l0, c1, l1)
    js = scl.ler(c0 // 2, l0 // 2, -(-c1 // 2), -(-l1 // 2))
    ws = -(-c1 // 2) - c0 // 2
    off = cena["offset"]
    soma = n = verdes = 0
    for col, lin in pts:
        if js[(lin // 2 - l0 // 2) * ws + (col // 2 - c0 // 2)] not in SCL_LIMPO:
            continue
        i = (lin - l0) * w + (col - c0)
        r, v = jr[i] - off, jn[i] - off
        if r + v <= 0:
            continue
        x = (v - r) / (v + r)
        soma += x
        n += 1
        verdes += x >= 0.4
    if n == 0:
        return {"ndvi": None, "verde_pct": None}
    return {"ndvi": round(soma / n, 2), "verde_pct": round(100 * verdes / n)}


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

def _png(largura: int, altura: int, rgb: bytes, filtro_sub: bool = False) -> bytes:
    def pedaco(tipo: bytes, dados: bytes) -> bytes:
        return struct.pack(">I", len(dados)) + tipo + dados + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF)
    linha = largura * 3
    partes = []
    for i in range(altura):
        l = rgb[i * linha:(i + 1) * linha]
        if filtro_sub:
            # filtro "Sub" (cada byte menos o do pixel a esquerda): a foto encolhe ~1/3;
            # no NDVI, de cores chapadas, atrapalha -- por isso e opcional.
            partes.append(b"\x01" + l[:3] + bytes((l[k] - l[k - 3]) & 255 for k in range(3, linha)))
        else:
            partes.append(b"\0" + l)
    bruto = b"".join(partes)
    return (b"\x89PNG\r\n\x1a\n" + pedaco(b"IHDR", struct.pack(">IIBBBBB", largura, altura, 8, 2, 0, 0, 0))
            + pedaco(b"IDAT", zlib.compress(bruto, 6)) + pedaco(b"IEND", b""))


def _imagens(cena: dict, pivos: list[dict], pasta: str, base: str) -> dict:
    """Escreve <base>-foto.png e <base>-ndvi.png da area dos pivos; devolve a geometria."""
    tci = Cog(cena["visual"])
    zona, sul = cena["zona"], cena["sul"]
    xs, ys = [], []
    for p in pivos:
        e, n = para_utm(p["lat"], p["lng"], zona, sul)
        xs += [e - p["raio"], e + p["raio"]]
        ys += [n - p["raio"], n + p["raio"]]
    c0, l1 = tci.pixel(min(xs) - MARGEM_M, min(ys) - MARGEM_M)
    c1, l0 = tci.pixel(max(xs) + MARGEM_M, max(ys) + MARGEM_M)
    c0, l0, c1, l1 = int(c0), int(l0), int(c1) + 1, int(l1) + 1
    fator = max(1, -(-max(c1 - c0, l1 - l0) // MAX_PX))
    w, h = (c1 - c0) // fator, (l1 - l0) // fator

    # foto (TCI ja vem em RGB 8 bits)
    jan = tci.ler(c0, l0, c0 + w * fator, l0 + h * fator)
    wl = w * fator
    foto = bytearray()
    for j in range(h):
        linha = jan[(j * fator) * wl * 3:(j * fator + 1) * wl * 3]
        if fator == 1:
            foto += linha.tobytes()
        else:
            for i in range(w):
                foto += linha[i * fator * 3:i * fator * 3 + 3].tobytes()
    with open(os.path.join(pasta, base + "-foto.png"), "wb") as f:
        f.write(_png(w, h, bytes(foto), filtro_sub=True))

    # NDVI colorido, nuvem em cinza, sem dado em preto
    red, nir, scl = Cog(cena["red"]), Cog(cena["nir"]), Cog(cena["scl"])
    jr = red.ler(c0, l0, c0 + w * fator, l0 + h * fator)
    jn = nir.ler(c0, l0, c0 + w * fator, l0 + h * fator)
    sc0, sl0 = c0 // 2, l0 // 2
    sw = -(-(c0 + w * fator) // 2) - sc0
    js = scl.ler(sc0, sl0, sc0 + sw, -(-(l0 + h * fator) // 2))
    off = cena["offset"]
    nd = bytearray()
    cinza, preto = bytes((200, 200, 200)), bytes((0, 0, 0))
    for j in range(h):
        lin = j * fator
        for i in range(w):
            col = i * fator
            s = js[((l0 + lin) // 2 - sl0) * sw + ((c0 + col) // 2 - sc0)]
            if s == 0:
                nd += preto
                continue
            if s not in SCL_LIMPO:
                nd += cinza
                continue
            k = lin * wl + col
            r, v = jr[k] - off, jn[k] - off
            if r + v <= 0:
                nd += preto
                continue
            x = (v - r) / (v + r)
            nd += bytes(LUT_NDVI[int((max(-1.0, min(1.0, x)) + 1) * 100)])
    with open(os.path.join(pasta, base + "-ndvi.png"), "wb") as f:
        f.write(_png(w, h, bytes(nd)))

    # posicao de cada pivo na imagem (para a pagina desenhar os circulos)
    marcas = {}
    for p in pivos:
        e, n = para_utm(p["lat"], p["lng"], zona, sul)
        cc, lc = tci.pixel(e, n)
        marcas[p["nome"]] = {"x": round((cc - c0) / fator, 1), "y": round((lc - l0) / fator, 1),
                             "r": round(p["raio"] / (10 * fator), 1)}
    return {"foto": f"satelite/{base}-foto.png", "ndvi": f"satelite/{base}-ndvi.png",
            "largura": w, "altura": h, "metros_por_pixel": 10 * fator, "marcas": marcas}


# ---------------------------------------------------------------------------
# Uma fazenda
# ---------------------------------------------------------------------------

def analisar_fazenda(nome: str, pivos: list[dict], pasta: str, anterior: dict | None = None) -> dict:
    """Devolve o bloco 'satelite' de uma fazenda:

    {cena, data, dias, nuvem_cena, imagem: {foto, ndvi, largura, altura, metros_por_pixel, marcas},
     pivos: [{nome, ndvi, verde_pct, classe, cor, data, dias, cena, limpo_pct}],
     resumo: {vigoroso, medio, fraco, solo, nublado}, cena_mais_nova, analisado_em, avisos}

    'anterior' e o resultado da ultima vez: se o catalogo nao tem cena nova
    desde entao, ele volta como esta (so os 'dias' sao recontados).
    """
    base = slug(nome)
    lats, lngs = [p["lat"] for p in pivos], [p["lng"] for p in pivos]
    bbox = [min(lngs) - 0.01, min(lats) - 0.01, max(lngs) + 0.01, max(lats) + 0.01]
    cenas = buscar_cenas(bbox)
    hoje = datetime.now(TZ_BR).date()
    if not cenas:
        return {"pivos": [], "avisos": [f"nenhuma cena Sentinel-2 nos ultimos {DIAS_DE_BUSCA} dias"],
                "analisado_em": datetime.now(TZ_BR).isoformat()}
    if anterior and anterior.get("cena_mais_nova") == cenas[0]["id"] and anterior.get("imagem") \
            and all(os.path.exists(os.path.join(pasta, os.path.basename(anterior["imagem"][k]))) for k in ("foto", "ndvi")):
        return _recontar_dias(anterior, hoje)

    log(f"{nome}: {len(pivos)} pivos, {len(cenas)} cenas desde {cenas[-1]['data']}; cena mais nova {cenas[0]['data']}")
    resultado: dict[str, dict] = {}
    aceitas: dict[str, int] = {}
    cogs: dict[str, Cog] = {}
    avisos: list[str] = []

    def cog(url: str) -> Cog:
        if url not in cogs:
            cogs[url] = Cog(url)
        return cogs[url]

    for cena in cenas:
        pendentes = [p for p in pivos if p["nome"] not in resultado and _dentro(cena, p["lat"], p["lng"])]
        if not pendentes:
            continue
        try:
            scl = cog(cena["scl"])
            for p in pendentes:
                limpo, cobre = _nuvem_no_pivo(scl, p, cena)
                if not cobre or limpo < LIMPO_MIN:
                    continue
                r = _ndvi_do_pivo(cog(cena["red"]), cog(cena["nir"]), scl, p, cena)
                if r["ndvi"] is None:
                    continue
                cl, cor = classe(r["ndvi"])
                resultado[p["nome"]] = {"nome": p["nome"], **r, "classe": cl, "cor": cor, "data": cena["data"],
                                        "cena": cena["id"], "limpo_pct": round(100 * limpo)}
                aceitas[cena["id"]] = aceitas.get(cena["id"], 0) + 1
        except Exception as e:  # noqa: BLE001
            avisos.append(f"cena {cena['id']}: {e}")
            log(f"{nome}: erro na cena {cena['id']}: {e}")
        if len(resultado) == len(pivos):
            break

    # Imagem: a cena que serviu para mais pivos (empate: a mais nova). Se nenhuma
    # serviu, a menos nublada entre as 10 mais novas -- para se ver o porque.
    if aceitas:
        escolhida = max(cenas, key=lambda c: (aceitas.get(c["id"], 0), c["quando"]))
    else:
        escolhida = min(cenas[:10], key=lambda c: (c["nuvem"] if c["nuvem"] is not None else 101))
        avisos.append(f"nenhum pivo ficou sem nuvem em {DIAS_DE_BUSCA} dias; a imagem e a menos nublada")

    imagem = None
    if anterior and anterior.get("cena") == escolhida["id"] and anterior.get("imagem") \
            and all(os.path.exists(os.path.join(pasta, os.path.basename(anterior["imagem"][k]))) for k in ("foto", "ndvi")) \
            and set(anterior["imagem"].get("marcas", {})) == {p["nome"] for p in pivos}:
        imagem = anterior["imagem"]
    else:
        try:
            os.makedirs(pasta, exist_ok=True)
            imagem = _imagens(escolhida, pivos, pasta, base)
        except Exception as e:  # noqa: BLE001
            avisos.append(f"imagem: {e}")
            log(f"{nome}: erro ao montar a imagem: {e}")

    lista = []
    for p in pivos:
        r = resultado.get(p["nome"])
        if r:
            lista.append(r)
        else:
            cl, cor = classe(None)
            lista.append({"nome": p["nome"], "ndvi": None, "verde_pct": None, "classe": cl, "cor": cor,
                          "data": None, "cena": None, "limpo_pct": None})
    resumo = {c[1]: 0 for c in CLASSES}
    resumo["nublado"] = 0
    for r in lista:
        resumo[r["classe"]] += 1
    saida = {
        "cena": escolhida["id"], "data": escolhida["data"], "nuvem_cena": escolhida["nuvem"],
        "imagem": imagem, "pivos": lista, "resumo": resumo,
        "cena_mais_nova": cenas[0]["id"], "data_mais_nova": cenas[0]["data"],
        "analisado_em": datetime.now(TZ_BR).isoformat(), "avisos": avisos,
    }
    return _recontar_dias(saida, hoje)


def _recontar_dias(s: dict, hoje) -> dict:
    def dias(d):
        return None if not d else (hoje - datetime.strptime(d, "%Y-%m-%d").date()).days
    s["dias"] = dias(s.get("data"))
    for p in s.get("pivos", []):
        p["dias"] = dias(p.get("data"))
    return s


# ---------------------------------------------------------------------------
# Todas as fazendas, com cache em disco
# ---------------------------------------------------------------------------

class Satelite:
    """Guarda satelite.json na pasta e refaz a analise a cada 'a_cada_h' horas.

    atualizar() e para rodar em segundo plano (demora minutos na primeira vez:
    baixa alguns MB por fazenda). dados() devolve o ultimo resultado, na hora.
    """

    def __init__(self, cfg: dict, pasta: str = PASTA_PADRAO, a_cada_h: float = 3):
        self.cfg = cfg
        self.pasta = pasta
        self.a_cada_s = a_cada_h * 3600
        self.arquivo = os.path.join(pasta, "satelite.json")
        self._dados = {"fazendas": {}, "atualizado_em": None}
        if os.path.exists(self.arquivo):
            try:
                with open(self.arquivo, encoding="utf-8") as f:
                    self._dados = json.load(f)
            except Exception:  # noqa: BLE001
                pass

    def dados(self) -> dict:
        hoje = datetime.now(TZ_BR).date()
        for s in self._dados.get("fazendas", {}).values():
            _recontar_dias(s, hoje)
        return self._dados

    def precisa(self) -> bool:
        q = self._dados.get("atualizado_em")
        if not q:
            return True
        # fazenda que esta no mapa (pivos.json) mas ainda nao foi analisada: nao espera o prazo
        try:
            with open(os.path.join(self.pasta, "pivos.json"), encoding="utf-8") as f:
                pivos = json.load(f)
        except (OSError, ValueError):
            pivos = {}
        if any(lista and nome not in self._dados.get("fazendas", {}) for nome, lista in pivos.items()):
            return True
        try:
            return (datetime.now(TZ_BR) - datetime.fromisoformat(q)).total_seconds() >= self.a_cada_s
        except ValueError:
            return True

    def atualizar(self, forcar: bool = False, so: str | None = None) -> dict:
        if not forcar and not self.precisa():
            return self.dados()
        os.makedirs(self.pasta, exist_ok=True)
        pivos = pivos_do_mapa(self.cfg, os.path.join(self.pasta, "pivos.json"))
        fazendas = self._dados.setdefault("fazendas", {})
        for nome, lista in pivos.items():
            if so and so.lower() not in nome.lower():
                continue
            if not lista:
                continue
            t0 = time.time()
            try:
                fazendas[nome] = analisar_fazenda(nome, lista, self.pasta, fazendas.get(nome))
                fazendas[nome]["total_pivos"] = len(lista)
                log(f"{nome}: {fazendas[nome]['resumo']} em {time.time() - t0:.0f} s"
                    if "resumo" in fazendas[nome] else f"{nome}: sem cena")
            except Exception as e:  # noqa: BLE001
                log(f"{nome}: falhou ({e})")
                fazendas.setdefault(nome, {"pivos": []})["avisos"] = [str(e)]
            self._gravar()
        # fazenda que sumiu do mapa some daqui tambem
        for nome in list(fazendas):
            if nome not in pivos:
                del fazendas[nome]
        self._dados["atualizado_em"] = datetime.now(TZ_BR).isoformat()
        self._gravar()
        return self.dados()

    def _gravar(self):
        tmp = self.arquivo + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._dados, f, ensure_ascii=False)
        os.replace(tmp, self.arquivo)


def aplicar(estado: dict, dados: dict):
    """Pendura o bloco 'satelite' em cada fazenda do estado (pelo nome)."""
    por_nome = dados.get("fazendas", {}) if dados else {}
    for f in estado.get("fazendas", []):
        s = por_nome.get(f["nome"])
        if s:
            f["satelite"] = s
    estado["satelite_atualizado_em"] = (dados or {}).get("atualizado_em")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Sentinel-2 por fazenda")
    ap.add_argument("--fazenda", help="parte do nome (ex.: Divina)")
    ap.add_argument("--pasta", default=PASTA_PADRAO)
    args = ap.parse_args()
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    d = Satelite(cfg, args.pasta).atualizar(forcar=True, so=args.fazenda)
    for nome, s in d["fazendas"].items():
        if args.fazenda and args.fazenda.lower() not in nome.lower():
            continue
        print(f"\n{nome}: imagem de {s.get('data')} ({s.get('dias')} dias), nuvem da cena {s.get('nuvem_cena')} %")
        for p in s.get("pivos", []):
            print(f"  {p['nome']:<10} NDVI {p['ndvi'] if p['ndvi'] is not None else '  - ':<5} {p['classe']:<9} "
                  f"{p['data'] or 'sem cena limpa'}  verde {p['verde_pct'] if p['verde_pct'] is not None else '-'} %")
        for a in s.get("avisos", []):
            print("  aviso:", a)
