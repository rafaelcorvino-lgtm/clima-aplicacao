#!/usr/bin/env python3
"""Cliente das estacoes Zeus Agrotech (portal.zeusagro.com).

Fala com a mesma API que o portal usa no navegador, do mesmo jeito que o
portal faz: login no AWS Cognito (o servico de identidade da Amazon que a
Zeus usa) e depois chamadas HTTPS com o token recebido.

    https://api.zeusagro.com/portal/v1/pics                  lista de estacoes
    https://api.zeusagro.com/portal/v1/pic-data/last-frame/{id}  ultima leitura
    https://api.zeusagro.com/portal/v1/pics/alert/SPRAY_CURRENT  parecer da Zeus

Nada sai daqui para outro lugar: e-mail e senha vao apenas para o Cognito da
Zeus (cognito-idp.us-east-1.amazonaws.com), por HTTPS, e o token resultante vai
apenas para api.zeusagro.com. So biblioteca padrao do Python.

Login tenta primeiro o fluxo simples (USER_PASSWORD_AUTH). Se o pool da Zeus
nao permitir, cai no fluxo SRP (o que o navegador usa), implementado abaixo.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.zeusagro.com/portal/v1"
REGIAO = "us-east-1"
POOL_ID = "us-east-1_3CS6GIMei"
CLIENT_ID = "5ro0i6pcrp9f4e3icb2iar8jdf"
COGNITO = f"https://cognito-idp.{REGIAO}.amazonaws.com/"
TIMEOUT = 25


class ErroZeus(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP minimo
# ---------------------------------------------------------------------------

def _post_json(url: str, corpo: dict, cabecalhos: dict) -> dict:
    dados = json.dumps(corpo).encode()
    req = urllib.request.Request(url, data=dados, method="POST")
    for k, v in cabecalhos.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        texto = e.read().decode(errors="replace")
        try:
            return {"__erro__": e.code, **json.loads(texto)}
        except Exception:
            return {"__erro__": e.code, "message": texto}


def _get_json(url: str, token: str):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode() or "null")


# ---------------------------------------------------------------------------
# Cognito: fluxo por senha e fluxo SRP
# ---------------------------------------------------------------------------

def _cognito(alvo: str, corpo: dict) -> dict:
    return _post_json(COGNITO, corpo, {
        "Content-Type": "application/x-amz-json-1.1",
        "X-Amz-Target": f"AWSCognitoIdentityProviderService.{alvo}",
    })


_N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183"
    "995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33A"
    "85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7A"
    "BF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864D"
    "87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E20"
    "8E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
_N = int(_N_HEX, 16)
_G = 2


def _pad_hex(valor) -> str:
    h = valor if isinstance(valor, str) else format(valor, "x")
    if len(h) % 2 == 1:
        h = "0" + h
    elif h[0] in "89abcdefABCDEF":
        h = "00" + h
    return h


def _hash_hex(hex_str: str) -> str:
    d = hashlib.sha256(bytes.fromhex(hex_str)).hexdigest()
    return "0" * (64 - len(d)) + d


def _hash_bytes(b: bytes) -> str:
    d = hashlib.sha256(b).hexdigest()
    return "0" * (64 - len(d)) + d


def _hkdf(ikm: bytes, sal: bytes) -> bytes:
    prk = hmac.new(sal, ikm, hashlib.sha256).digest()
    info = b"Caldera Derived Key" + b"\x01"
    return hmac.new(prk, info, hashlib.sha256).digest()[:16]


def _timestamp_cognito() -> str:
    # Formato exigido pelo Cognito, sempre em ingles, dia sem zero a esquerda.
    dias = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    meses = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    n = datetime.now(timezone.utc)
    return f"{dias[n.weekday()]} {meses[n.month - 1]} {n.day} {n:%H:%M:%S} UTC {n.year}"


def _login_srp(email: str, senha: str) -> dict:
    k = int(_hash_hex(_pad_hex(_N_HEX) + _pad_hex(_G)), 16)
    a = int.from_bytes(os.urandom(128), "big") % _N
    A = pow(_G, a, _N)

    r1 = _cognito("InitiateAuth", {
        "AuthFlow": "USER_SRP_AUTH",
        "ClientId": CLIENT_ID,
        "AuthParameters": {"USERNAME": email, "SRP_A": format(A, "x")},
    })
    if "__erro__" in r1:
        raise ErroZeus(f"Cognito recusou o inicio do login: {r1.get('message')}")
    if r1.get("ChallengeName") != "PASSWORD_VERIFIER":
        raise ErroZeus(f"Cognito pediu um desafio inesperado: {r1.get('ChallengeName')}")

    p = r1["ChallengeParameters"]
    B = int(p["SRP_B"], 16)
    sal = p["SALT"]
    bloco = p["SECRET_BLOCK"]
    usuario = p["USER_ID_FOR_SRP"]
    if B % _N == 0:
        raise ErroZeus("SRP_B invalido")

    pool = POOL_ID.split("_")[1]
    u = int(_hash_hex(_pad_hex(A) + _pad_hex(B)), 16)
    if u == 0:
        raise ErroZeus("u invalido")
    x = int(_hash_hex(_pad_hex(sal) + _hash_bytes(f"{pool}{usuario}:{senha}".encode())), 16)
    S = pow((B - k * pow(_G, x, _N)) % _N, a + u * x, _N)
    chave = _hkdf(bytes.fromhex(_pad_hex(S)), bytes.fromhex(_pad_hex(u)))

    ts = _timestamp_cognito()
    msg = pool.encode() + usuario.encode() + base64.b64decode(bloco) + ts.encode()
    assinatura = base64.b64encode(hmac.new(chave, msg, hashlib.sha256).digest()).decode()

    r2 = _cognito("RespondToAuthChallenge", {
        "ChallengeName": "PASSWORD_VERIFIER",
        "ClientId": CLIENT_ID,
        "ChallengeResponses": {
            "TIMESTAMP": ts,
            "USERNAME": usuario,
            "PASSWORD_CLAIM_SECRET_BLOCK": bloco,
            "PASSWORD_CLAIM_SIGNATURE": assinatura,
        },
    })
    if "__erro__" in r2:
        raise ErroZeus(f"Login recusado: {r2.get('message')}")
    if "AuthenticationResult" not in r2:
        raise ErroZeus(f"Cognito pediu mais um passo ({r2.get('ChallengeName')}); "
                       "resolva no portal (troca de senha ou MFA) e tente de novo")
    return r2["AuthenticationResult"]


def _login_senha(email: str, senha: str) -> dict | None:
    r = _cognito("InitiateAuth", {
        "AuthFlow": "USER_PASSWORD_AUTH",
        "ClientId": CLIENT_ID,
        "AuthParameters": {"USERNAME": email, "PASSWORD": senha},
    })
    if "__erro__" in r:
        msg = str(r.get("message", ""))
        if "not enabled" in msg or r.get("__type") == "InvalidParameterException":
            return None  # fluxo nao habilitado no pool: usar SRP
        raise ErroZeus(f"Login recusado: {msg}")
    if "AuthenticationResult" not in r:
        raise ErroZeus(f"Cognito pediu mais um passo ({r.get('ChallengeName')})")
    return r["AuthenticationResult"]


def _renovar(refresh_token: str) -> dict | None:
    r = _cognito("InitiateAuth", {
        "AuthFlow": "REFRESH_TOKEN_AUTH",
        "ClientId": CLIENT_ID,
        "AuthParameters": {"REFRESH_TOKEN": refresh_token},
    })
    if "__erro__" in r or "AuthenticationResult" not in r:
        return None
    return r["AuthenticationResult"]


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _primeiro(d: dict, *chaves):
    for c in chaves:
        if c in d and d[c] is not None:
            return d[c]
    return None


def _acha(d, *trechos):
    """Procura a primeira chave do dicionario cujo nome contenha algum trecho."""
    if not isinstance(d, dict):
        return None
    for t in trechos:
        for k, v in d.items():
            if t.lower() in k.lower() and v is not None:
                return v
    return None


def _data_iso(v):
    """Aceita epoch (s ou ms) ou texto ISO e devolve texto ISO em UTC."""
    if v is None:
        return None
    try:
        n = float(v)
        if n > 1e11:
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    try:
        s2 = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(s2)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat()
    except ValueError:
        return s


class ClienteZeus:
    def __init__(self, email: str, senha: str, arquivo_sessao: str | None = None):
        self.email = email
        self.senha = senha
        self.arquivo_sessao = arquivo_sessao
        self._tokens: dict | None = None
        self._expira = 0.0
        self._qual_token = "IdToken"  # o portal manda o IdToken; se a API recusar, tenta AccessToken
        self._carregar_sessao()

    # --- sessao -----------------------------------------------------------
    def _carregar_sessao(self):
        if not self.arquivo_sessao or not os.path.exists(self.arquivo_sessao):
            return
        try:
            with open(self.arquivo_sessao, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("email") == self.email:
                self._tokens = d.get("tokens")
                self._expira = float(d.get("expira", 0))
                self._qual_token = d.get("qual_token", "IdToken")
        except Exception:
            pass

    def _salvar_sessao(self):
        if not self.arquivo_sessao:
            return
        try:
            with open(self.arquivo_sessao, "w", encoding="utf-8") as f:
                json.dump({"email": self.email, "tokens": self._tokens,
                           "expira": self._expira, "qual_token": self._qual_token}, f)
        except Exception:
            pass

    def _guardar(self, res: dict):
        if self._tokens and "RefreshToken" not in res:
            res["RefreshToken"] = self._tokens.get("RefreshToken")
        self._tokens = res
        self._expira = time.time() + float(res.get("ExpiresIn", 3600)) - 120
        self._salvar_sessao()

    def entrar(self):
        res = _login_senha(self.email, self.senha)
        if res is None:
            res = _login_srp(self.email, self.senha)
        self._guardar(res)

    def token(self) -> str:
        if self._tokens and time.time() < self._expira:
            return self._tokens[self._qual_token]
        if self._tokens and self._tokens.get("RefreshToken"):
            novo = _renovar(self._tokens["RefreshToken"])
            if novo:
                self._guardar(novo)
                return self._tokens[self._qual_token]
        self.entrar()
        return self._tokens[self._qual_token]

    # --- chamadas ---------------------------------------------------------
    def _get(self, caminho: str):
        url = API + caminho
        try:
            return _get_json(url, self.token())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # Primeiro tenta o outro tipo de token, depois um login novo.
                outro = "AccessToken" if self._qual_token == "IdToken" else "IdToken"
                if self._tokens and outro in self._tokens:
                    try:
                        r = _get_json(url, self._tokens[outro])
                        self._qual_token = outro
                        self._salvar_sessao()
                        return r
                    except urllib.error.HTTPError:
                        pass
                self._tokens = None
                return _get_json(url, self.token())
            raise ErroZeus(f"API Zeus respondeu {e.code} em {caminho}")

    # Formato real conferido em 2026-10-23 (46 estacoes do Rafael):
    #   /pics -> [{picId, name, geom:{lat,lng}, location, lastDataUpdate,
    #              communicationStatus: OK|INTERMITTENT, picType:{slug}, clusters:[{name}],
    #              dataTimeFrame: TIME_FRAME_60M, hasWindSensor, inactive, visible, testMode}]
    #   /pic-data/last-frame/{id} -> quadro da ultima hora: started/finished (UTC),
    #              temperatureInst/Min/Max, humidityInst/Min/Max, windSpeedInst,
    #              windSpeedAverage, windDirectionInst/Average, sustainedGustSpeed,
    #              gustSpeed, rainMain, atmosphericPressureInst, solarIrradianceInst
    #   /pics/alert/SPRAY_CURRENT e SPRAY_FORECAST -> [{picId, alertLevel: GREEN|YELLOW|RED,
    #              sprayAlertWindow:[{windowTimeFrame (UTC), alertLevel, alertCause:[wind|temperature|relative_humidity]}]}]
    # O portal mostra windSpeedAverage como "Vento" e sustainedGustSpeed como "Rajada"; seguimos igual.

    def estacoes(self) -> list[dict]:
        bruto = self._get("/pics")
        itens = bruto if isinstance(bruto, list) else (
            bruto.get("items") or bruto.get("data") or bruto.get("pics") or bruto.get("content") or [])
        saida = []
        for p in itens:
            if not isinstance(p, dict):
                continue
            geom = p.get("geom") if isinstance(p.get("geom"), dict) else {}
            tipo = p.get("picType")
            clusters = [str(c.get("name", "")).strip() for c in (p.get("clusters") or []) if isinstance(c, dict)]
            saida.append({
                "id": _primeiro(p, "picId", "id"),
                "nome": str(_primeiro(p, "name", "picName") or f"PIC {p.get('picId')}").strip(),
                "clusters": clusters,
                "local": p.get("location"),
                "lat": _num(_primeiro(geom, "lat", "latitude") or _primeiro(p, "latitude", "lat")),
                "lng": _num(_primeiro(geom, "lng", "longitude") or _primeiro(p, "longitude", "lng")),
                "tipo": tipo.get("slug") if isinstance(tipo, dict) else tipo,
                "tem_vento": bool(p.get("hasWindSensor")),
                "comunicacao": p.get("communicationStatus"),
                "ultimo_envio": _data_iso(p.get("lastDataUpdate")),
                "quadro": p.get("dataTimeFrame"),
                "desativada": bool(p.get("inactive") or p.get("testMode") or p.get("visible") is False),
                "_bruto": p,
            })
        return saida

    def ultima_leitura(self, pic_id, tem_vento: bool = True) -> dict:
        b = self._get(f"/pic-data/last-frame/{pic_id}")
        if isinstance(b, dict) and isinstance(b.get("data"), dict):
            b = b["data"]
        if not isinstance(b, dict):
            return {"_bruto": b}
        vento = rajada = vento_inst = direcao = None
        if tem_vento:
            vento = _num(_primeiro(b, "windSpeedAverage", "windSpeed", "wind"))
            vento_inst = _num(b.get("windSpeedInst"))
            rajada = _num(_primeiro(b, "sustainedGustSpeed", "gustSpeed", "gust"))
            direcao = _num(_primeiro(b, "windDirectionAverage", "windDirectionInst", "windDirection"))
        return {
            "temperatura": _num(_primeiro(b, "temperatureInst", "temperature")),
            "temperatura_max": _num(b.get("temperatureMax")),
            "umidade": _num(_primeiro(b, "humidityInst", "humidity")),
            "umidade_min": _num(b.get("humidityMin")),
            "vento": vento,
            "vento_inst": vento_inst,
            "rajada": rajada,
            "direcao": direcao,
            "chuva_hora": _num(_primeiro(b, "rainMain", "rain")),
            "pressao": _num(_primeiro(b, "atmosphericPressureInst", "atmosphericPressure")),
            "radiacao": _num(b.get("solarIrradianceInst")),
            "hora": _data_iso(_primeiro(b, "finished", "timestamp", "date") or _acha(b, "finish", "date", "time")),
            "hora_inicio": _data_iso(b.get("started")),
            "_bruto": b,
        }

    @staticmethod
    def _janelas(alerta: dict) -> list[dict]:
        saida = []
        for w in alerta.get("sprayAlertWindow") or []:
            if not isinstance(w, dict):
                continue
            saida.append({
                "hora": _data_iso(w.get("windowTimeFrame")),
                "nivel": w.get("alertLevel"),
                "causas": [CAUSAS.get(c, c) for c in (w.get("alertCause") or [])],
            })
        return saida

    def parecer_pulverizacao(self) -> dict:
        """Alerta SPRAY_CURRENT da propria Zeus, por estacao: {picId: {nivel, causas, hora}}."""
        try:
            b = self._get("/pics/alert/SPRAY_CURRENT")
        except ErroZeus:
            return {}
        saida = {}
        for a in (b if isinstance(b, list) else []):
            if not isinstance(a, dict):
                continue
            jan = self._janelas(a)
            saida[str(a.get("picId"))] = {
                "nivel": a.get("alertLevel"),
                "causas": jan[0]["causas"] if jan else [],
                "hora": jan[0]["hora"] if jan else _data_iso(a.get("created")),
            }
        return saida

    def previsao_pulverizacao(self) -> dict:
        """Alerta SPRAY_FORECAST: janelas de 3 h por estacao, {picId: [{hora, nivel, causas}]}."""
        try:
            b = self._get("/pics/alert/SPRAY_FORECAST")
        except ErroZeus:
            return {}
        saida = {}
        for a in (b if isinstance(b, list) else []):
            if isinstance(a, dict):
                saida[str(a.get("picId"))] = self._janelas(a)
        return saida


CAUSAS = {"wind": "vento", "temperature": "temperatura", "relative_humidity": "umidade",
          "rain": "chuva", "delta_t": "delta T"}


def carregar_segredos(caminho: str) -> dict:
    """Le um arquivo CHAVE=valor (segredos.txt). Linhas com # sao comentarios."""
    d = {}
    if not os.path.exists(caminho):
        return d
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            k, v = linha.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    return d


if __name__ == "__main__":
    # Teste rapido: python zeus.py  (le segredos.txt da mesma pasta)
    import sys
    aqui = os.path.dirname(os.path.abspath(__file__))
    seg = carregar_segredos(os.path.join(aqui, "segredos.txt"))
    if not seg.get("ZEUS_EMAIL") or not seg.get("ZEUS_SENHA"):
        print("Preencha ZEUS_EMAIL e ZEUS_SENHA em segredos.txt")
        sys.exit(1)
    c = ClienteZeus(seg["ZEUS_EMAIL"], seg["ZEUS_SENHA"], os.path.join(aqui, ".sessao-zeus.json"))
    ests = c.estacoes()
    print(f"{len(ests)} estacao(oes):")
    for e in ests:
        print(f"  [{e['id']}] {e['nome']:<22} vento={'sim' if e['tem_vento'] else 'nao'} {e['tipo']:<16} "
              f"{e['comunicacao']} clusters={e['clusters']} lat={e['lat']} lng={e['lng']}")
    if ests and "--bruto" in sys.argv:
        print(json.dumps(ests[0]["_bruto"], indent=2, ensure_ascii=False)[:3000])
    parecer = c.parecer_pulverizacao()
    for e in ests[:60]:
        if e["desativada"] or e["id"] is None:
            continue
        try:
            l = c.ultima_leitura(e["id"], e["tem_vento"])
        except Exception as ex:
            print(f"  {e['nome']}: erro {ex}")
            continue
        z = parecer.get(str(e["id"])) or {}
        print(f"  {e['nome']:<22} T={l['temperatura']} UR={l['umidade']} vento={l['vento']} raj={l['rajada']} "
              f"dir={l['direcao']} hora={l['hora']} zeus={z.get('nivel')} {z.get('causas')}")
        if "--bruto" in sys.argv:
            print(json.dumps(l["_bruto"], indent=2, ensure_ascii=False)[:3000])
