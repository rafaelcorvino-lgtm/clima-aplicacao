# Clima de Aplicação

Diz, fazenda por fazenda, se está **bom**, **atenção** ou **ruim** para aplicação
aérea agora, usando a leitura das estações Zeus (portal.zeusagro.com) e a
previsão das próximas horas. Abre no celular.

## Critério (config.json)

| Variável | Bom | Atenção | Ruim |
|---|---|---|---|
| Temperatura | até 28 °C | 28 a 30 °C | acima de 30 °C |
| Vento | 4,5 a 13,5 km/h | 3 a 4,5 ou 13,5 a 15 km/h | abaixo de 3 ou acima de 15 km/h |
| Umidade | acima de 55 % | 50 a 55 % | abaixo de 50 % |
| Rajada (só estação) | até 15 km/h | acima de 15 km/h | nunca reprova sozinha (`rajada_reprova`) |
| Delta T (calculado) | 2 a 8 | abaixo de 2 ou acima de 8 | nunca reprova sozinho (`delta_t_reprova`) |

O estado da estação é o pior dos itens. O estado da **fazenda** é o da pior
estação com sensor de vento (a fazenda é grande; se um ponto está ruim, o
piloto precisa ver). Estação de vento sem termo-higrômetro (São Jorge) pega
temperatura e umidade da vizinha mais recente e avisa. Leitura com mais de
90 min vira "LEITURA ANTIGA" e não decide. Tudo isso é ajustável em
`config.json` (`limites`). As margens de "atenção" são só um aviso antes do
limite; os limites de fato são os do piloto: 30 °C, 3 a 15 km/h, 50 %.

## As estações do Rafael (conferido em 23/10/2026)

46 estações Zeus, em 4 fazendas (agrupadas pela posição, dentro de 40 km das
coordenadas do `config.json`):

| Fazenda | Estações com vento | Só temperatura/umidade |
|---|---|---|
| Serra Bonita (Unaí) | Pv 1, Pv30, S21, S2A, S8, Sede São Miguel 1 | 18 |
| Divina | Sede Fazenda Divina, PV 6 e 7 | 6 |
| Chapadão do Bugre | TALHÃO 20A | 7 |
| Três Marias (Casa Branca) | São Jorge (só vento) | 5 |

Larga Grande, Santa Marta e Canaã não têm estação Zeus: só previsão.

A Zeus fecha **um quadro por hora** (`TIME_FRAME_60M`): a leitura das 10:00
chega por volta das 10:02. "Vento" é a média da hora (como no portal),
"inst" é o último instante, "raj" é a rajada sustentada. A Zeus também
calcula o próprio parecer de pulverização (verde/amarelo/vermelho, com a
causa) agora e em janelas de 3 h; a página mostra os dois.

## Fontes

- **Estações Zeus**: a mesma API que o portal usa no navegador
  (`api.zeusagro.com/portal/v1`), com o login do portal (AWS Cognito). E-mail e
  senha vão só para o login da Zeus, por HTTPS. O token fica em
  `.sessao-zeus.json` e é renovado sozinho.
- **Previsão**: Open-Meteo (`api.open-meteo.com`), serviço aberto, sem chave.
  Só latitude e longitude saem daqui. Usada para "próxima janela" e para
  fazenda sem estação (aí a faixa diz "previsão").
- **Delta T**: calculado da temperatura e umidade (bulbo úmido por Stull 2011).
- **Satélite**: Sentinel-2 L2A pelo Earth Search / AWS (aberto, sem chave) e
  pivôs do mapa MariAgro (Firebase, leitura pública). Ver a seção "Satélite".

## Como rodar

1. Copie `segredos_exemplo.txt` para `segredos.txt` e preencha `ZEUS_EMAIL` e
   `ZEUS_SENHA` (e `APP_SENHA` se for abrir de fora da rede).
2. Teste o acesso à Zeus:

   ```
   python aplicacao-aerea/zeus.py
   ```

   Lista as estações e a última leitura de cada uma.
3. Suba o servidor:

   ```
   python aplicacao-aerea/servidor.py --porta 8090
   ```

   Abra `http://localhost:8090` (ou o IP do notebook, no celular).
   No Claude Code: preview `clima-aplicacao`.
4. Sem credenciais, para ver a tela: `--simular` (ou preview
   `clima-aplicacao-simulado`).

## Previsão da área × estações da fazenda (histórico)

A Zeus fecha um quadro por hora; entre um quadro e outro só existe a
previsão. Para saber quanto confiar nela em cada fazenda, o app compara, a
cada quadro, a **previsão da área** (Open-Meteo no centro das estações da
fazenda) com o **conjunto das estações** daquela fazenda no mesmo quadro:
média, mínimo e máximo de temperatura e umidade (todas as estações) e de
vento (as que têm sensor). Fica em `historico.db` (SQLite). Na tela, por
fazenda:

- tabela "Previsão da área × estações, quadro das 12:00": previsto, média
  das estações com a faixa, e a diferença (previsão menos média; amarelo a
  partir de 1 °C / 3 km/h / 3 %, vermelho a partir de 2 °C / 5 km/h / 5 %);
- "Últimas 24 h": erro médio, tendência (viés) e pior caso, por variável.

Série completa de uma fazenda: `GET /api/historico?fazenda=Faz.%20Divina&horas=48`.

Primeira amostra (23/10/2026, 12:00): a previsão veio 2 a 4 °C mais quente e
22 a 25 pontos mais seca que as estações em Divina, Serra Bonita e Três
Marias (em Chapadão acertou a umidade e errou o vento em 12 km/h). Para
umidade, a previsão NÃO substitui a estação.

## Satélite: imagem Sentinel-2 e NDVI por pivô

O Google Earth mostra a foto de 2023. O **Sentinel-2** (ESA/Copernicus) passa
a cada 5 dias com 10 m por pixel: não se vê o pivô nítido, mas se vê o
círculo verde ou seco, falha de irrigação e a evolução da cultura. Por fazenda,
a página mostra a área dos pivôs na **cena limpa mais recente** (botão Foto /
NDVI), com os círculos desenhados por cima, e uma lista com o **NDVI médio de
cada pivô**:

| NDVI | Classe |
|---|---|
| abaixo de 0,20 | solo exposto |
| 0,20 a 0,40 | fraco |
| 0,40 a 0,60 | médio |
| acima de 0,60 | vigoroso |

Como funciona (`satelite.py`, só biblioteca padrão):

- **Pivôs**: do mapa MariAgro (Firebase, leitura pública), os círculos
  desenhados lá como "pivô" (centro e raio), agrupados pelo nome da fazenda
  do `config.json` ou pela fazenda mais próxima. Só aparece aqui o que foi
  desenhado lá (em 30/10/2026: Larga 17, Divina 20, Serra Bonita 10, Três
  Marias 21, Chapadão 1).
- **Cenas**: catálogo STAC do Earth Search (Element 84) sobre o espelho
  Sentinel-2 L2A da AWS, aberto, sem chave. Os GeoTIFF são "cloud optimized":
  lê-se só os blocos de 1 km que cobrem a fazenda, com pedidos HTTP por faixa
  de bytes; a cena inteira (100 km × 100 km) nunca é baixada.
- **Nuvem**: pela máscara da própria cena (SCL, 20 m). Um pivô só recebe NDVI
  de uma cena em que ele está pelo menos 80 % limpo; senão a busca volta no
  tempo, pivô por pivô, até 60 dias. Pivô que ficou nublado 60 dias aparece
  cinza. A imagem é a cena que serviu para mais pivôs; se um pivô veio de
  outra data, a data aparece ao lado dele.
- **Ritmo**: fio em segundo plano, a cada 3 h (`satelite.a_cada_h` no
  config); só refaz quando o catálogo tem cena nova. A primeira vez leva
  1 a 2 min por fazenda (alguns MB). Cache em `satelite/` (satelite.json,
  PNGs, pivos.json); no robô do GitHub o cache mora em `site/satelite/`, então
  é publicado junto e sobrevive de uma execução para a outra.
- Para desligar: `"satelite": {"ativo": false}` no `config.json`.
- As imagens saem em grade latitude/longitude com os quatro limites no
  `satelite.json`, e `/satelite/` é servido com CORS: o **MariAgro** (botão
  Camada → "Sentinel") usa exatamente esses arquivos, do link público, como
  fundo do mapa por fazenda.

Teste na mão: `python aplicacao-aerea/satelite.py --fazenda Divina`.

Primeira leitura (cena de 20/10/2026): Divina com 8 pivôs vigorosos (NDVI
0,70 a 0,85), 3 fracos e 9 em solo exposto (0,12 a 0,17); Chapadão PV 08
vigoroso (0,84).

## Link público pelo GitHub (gratuito, sem notebook ligado)

Endereço: **https://rafaelcorvino-lgtm.github.io/clima-aplicacao/**

Como funciona: esta pasta é o repositório público
`rafaelcorvino-lgtm/clima-aplicacao`. Um robô (GitHub Actions,
`.github/workflows/atualizar.yml`) entra na Zeus, monta o estado com o mesmo
`estado.py` do notebook e publica no ramo `gh-pages`: `estado.json`,
`historico.db` e `index.html`.

**Desde 28/10/2026 a página é aberta, sem senha** (decisão do Rafael, para
compartilhar o link com a equipe): `CLIMA_ABERTO: "1"` no workflow. Qualquer
pessoa com o link vê os dados das fazendas. Para voltar a pedir senha, troque
para `"0"` e dê push: o robô passa a publicar `estado.enc`/`historico.db.enc`
cifrados com `APP_SENHA` (`nuvem/cifra.py`: PBKDF2 + AES-256-GCM) e a página
decifra no navegador com a senha. A página aceita os dois modos sozinha.

Mecanismo dos "primeiros minutos": a Zeus fecha o quadro às hh:00 e ele chega
por volta de hh:02. O robô é um **laço**: cada execução fica viva por 350 min
(quase o máximo de 6 h do GitHub) e publica em :02, :12, :22, :32, :42 e :52.
Nos primeiros 25 minutos da hora, se o quadro novo ainda não apareceu, tenta
a cada 60 s até ele chegar e só então publica. O agendamento (cron a cada 2 h)
serve apenas para iniciar a execução seguinte, que entra na fila e assume
quando a anterior termina.

Por que laço: medido de 23 a 25/10/2026, o cron do GitHub disparou cerca de
7 vezes por dia em vez das 144 pedidas (atrasa e pula). Com o laço, um atraso
de horas no cron não abre buraco.

Segredos (login Zeus e senha da página) ficam nos *secrets* do repositório,
enviados UMA vez pelo dono com `ENVIAR-SEGREDOS-GITHUB.bat`. Para disparar na
hora: `gh workflow run atualizar.yml --repo rafaelcorvino-lgtm/clima-aplicacao`.
Para republicar mudanças de código: `git push` nesta pasta.

## Link público pelo Firebase (PARADO: projeto sem cobrança ativa)

Endereço: **https://clima-aplicacao.web.app** (depois de publicado). Abre em
qualquer celular ou PC, sem instalar nada e sem depender do notebook ligado.
Pede a senha da página (a mesma `APP_SENHA`), que fica guardada no aparelho.

Como funciona: no projeto Firebase `calendario-aerea-26140` (o mesmo do
MariAgro e do monitor de pivôs) roda uma função agendada, `clima_atualizar`,
que a cada 10 min entra na Zeus, monta o estado com o MESMO código do
notebook (`estado.py`) e grava no Realtime Database em `/clima_aplicacao`.
A página estática no Hosting chama `/api/estado`, que a função `clima_api`
responde depois de conferir a senha. Login Zeus e senha ficam no Secret
Manager do Google, não no código.

Publicar (ou republicar depois de mudar algo):

```
python aplicacao-aerea/publicar.py
```

Na primeira vez, antes, enviar os segredos (uma vez só, na pasta `firebase/`;
cada comando pergunta o valor, cole o de `segredos.txt`):

```
firebase functions:secrets:set ZEUS_EMAIL
firebase functions:secrets:set ZEUS_SENHA
firebase functions:secrets:set APP_SENHA
```

Custo: a função roda 144 vezes por dia, uns 20 s cada; fica dentro da cota
gratuita do plano que o projeto já tem.

## No celular, pelo Tailscale

Tailscale é uma rede privada entre os seus aparelhos: o celular enxerga o
notebook de qualquer lugar, sem abrir porta no roteador e sem expor nada na
internet. Só quem está na sua conta Tailscale alcança a página, e ainda
assim ela pede a senha de `APP_SENHA`.

1. No notebook: instale o Tailscale (https://tailscale.com/download) e entre
   na conta (Google, por exemplo).
2. No celular (POCO X6 Pro): instale o app Tailscale da Play Store e entre
   na MESMA conta. Deixe a chave do Tailscale ligada.
3. No notebook, rode `INICIAR.bat`. Ele liga o `tailscale serve` (HTTPS com
   certificado válido) e imprime o endereço, algo como
   `https://nome-do-pc.algo.ts.net/`.
4. No celular, abra esse endereço, digite a senha de `APP_SENHA` e, no
   Chrome, "Adicionar à tela inicial" para virar ícone.

Para o app abrir sozinho quando o notebook ligar, rode uma vez
`INSTALAR-INICIO-AUTOMATICO.bat`. O app só responde com o notebook ligado.

## Arquivos

| Arquivo | O que faz |
|---|---|
| `iniciar.py` / `INICIAR.bat` | liga Tailscale serve + servidor e imprime o endereço do celular |
| `servidor.py` | servidor HTTP (só biblioteca padrão), junta tudo e serve a página |
| `zeus.py` | login Cognito (senha ou SRP) e chamadas `/pics`, `/pic-data/last-frame/{id}`, `/pics/alert/SPRAY_CURRENT` |
| `previsao.py` | previsão horária Open-Meteo |
| `regras.py` | avaliação bom/atenção/ruim e Delta T |
| `satelite.py` | Sentinel-2: cena limpa por fazenda, NDVI por pivô, PNGs em `satelite/` |
| `pagina.html` | a tela do celular, sem dependência externa |
| `config.json` | limites, fazendas (coordenadas do mapa MariAgro), intervalo |
| `segredos.txt` | credenciais (não vai para o git) |

## Pendências

- Leitura mais fresca que o quadro horário: o portal tem um relatório
  (`/reports/pic-data/gmt-3`) cujos parâmetros não foram descobertos; o
  quadro de 60 min é o que o plano atual entrega.
- Expor na internet para o celular: atrás de Tailscale ou túnel, com
  `APP_SENHA` e `APP_HTTPS=1`.
- Se as coordenadas de uma fazenda mudarem no MariAgro, atualizar
  `config.json` (o agrupamento é por distância).
