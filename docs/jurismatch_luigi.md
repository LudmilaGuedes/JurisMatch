> **Nota (19/10/2026):** este é o README original do repositório do Luigi
> (`LuigiAjello/JurisMatch`), guardado pelas decisões e números da coleta e do OCR do
> corpus inteiro. O projeto foi unificado em `Projeto_final/` — ver o `README.md` da raiz.
> Os caminhos mudaram:
>
> | Aqui | No projeto unificado |
> |---|---|
> | `dados/incidentes_stf.csv` | `metadata/incidentes_stf.csv` |
> | `dados/iniciais/` | `data/01_raw/iniciais_stf/` |
> | `dados/textos/` | `data/02_processed/textos_jsonl/` |
> | `dados/trechos/trechos.jsonl` | `data/03_chunks/chunks.jsonl` |
> | `dados/indice/` | `data/04_embeddings/` |
> | `dados/amostra/` | `data/amostra/` |

# JurisMatch

Busca semântica em petições iniciais do STF.

A ideia: dado um texto (uma tese, um trecho de petição, uma pergunta), encontrar as
petições iniciais de controle concentrado (ADI, ADC, ADO, ADPF) mais parecidas em
conteúdo — por significado, não só por palavra-chave.

## Estado atual

| Etapa | Situação |
|---|---|
| 1. Lista de processos | feito |
| 2. Download das petições iniciais | feito |
| 3. OCR / extração de texto | feito |
| 4. Limpeza e divisão em trechos | divisão feita (sobre a limpeza da Ludmila, 6 petições de teste) |
| 5. Encoder (embeddings) e índice vetorial | linha de base feita (`multilingual-e5-base`, sem treino) |
| 6. Busca (API / interface) | busca por linha de comando; interface a fazer |

## O que foi feito até agora

### 1. Lista de processos — `dados/incidentes_stf.csv`

906 processos de controle concentrado do STF, com o número do incidente (o
identificador que o portal usa nas URLs):

| Classe | Processos |
|---|---|
| ADI | 635 |
| ADPF | 236 |
| ADC | 20 |
| ADO | 15 |

Colunas: `processo, classe, numero, incidente, relator, data_autuacao, link`.

### 2. Download das petições iniciais — `scripts/baixar_iniciais_stf.py`

Para cada incidente, o script abre a lista de peças do processo eletrônico, acha
as peças cujo nome começa com "Petição inicial" e baixa o PDF de cada uma.

Resultado da carga completa (14–15/09/2026):

- **916 PDFs**, ~1,05 GB, de **900 processos**
  - 900 `_inicial.pdf`, 11 `_inicial_2.pdf`, 4 `_inicial_3.pdf` (processos com mais
    de uma peça chamada "Petição inicial") e 1 `_inicial_atualizada.pdf`
- **6 processos sem petição inicial** na lista de peças: ADI 3159, ADI 3596,
  ADI 4245, ADI 7548, ADI 7885 e ADPF 70
- nenhum erro de download pendente

O que o script resolve, e que vale saber antes de mexer:

- **WAF da AWS.** O portal devolve um desafio de JavaScript. O motor `requests`
  não passa; o motor `playwright` abre um Chromium com janela (headless leva 403),
  resolve o desafio e reaproveita o cookie `aws-waf-token` nas requisições seguintes.
- **Certificado.** O servidor do STF não envia o certificado intermediário
  (GlobalSign AlphaSSL). O `truststore` resolve no Python; para o Node do Playwright
  o script aponta `NODE_EXTRA_CA_CERTS` para `certs/stf_intermediario.pem`
  (certificado público, vence em 21/05/2027).
- **Educação com o servidor.** Uma sessão só, User-Agent fixo, pausa aleatória de
  4–9 s entre processos, pausa longa e nova tentativa em 403/429/503.
- **Retomada.** `dados/iniciais/_estado.json` é o checkpoint: dá para interromper e
  rodar de novo que ele pula o que já foi feito. `dados/iniciais/_log.csv` registra
  cada PDF (processo, incidente, status, arquivo, bytes, rótulo da peça).

### 3. Extração de texto e OCR — `scripts/extrair_texto.py`

Sai de `dados/iniciais/**/*.pdf` para um texto por página, em
`dados/textos/<CLASSE>/<mesmo nome do PDF>.jsonl` — uma linha por página:

```json
{"pagina": 3, "origem": "ocr", "chars_pdf": 0, "cobertura_imagem": 0.98, "texto_visivel": 0.0,
 "confianca_ocr": 91, "giro": 0, "texto": "...", "texto_figuras": ""}
```

| Campo | O que é |
|---|---|
| `origem` | `nativo` (o texto já estava no PDF), `ocr` (veio do Tesseract) ou `ocr_falhou` (texto vazio) |
| `chars_pdf`, `cobertura_imagem`, `texto_visivel` | os três números que decidem entre nativo e OCR — gravados para dar para auditar a decisão |
| `confianca_ocr` | confiança média do Tesseract na página (0–100); `null` se a página não passou pelo OCR |
| `giro` | quantos graus a página foi girada antes do OCR (scan de lado ou de cabeça para baixo) |
| `texto` | o texto da página |
| `texto_figuras` | só em página nativa: o texto que está **dentro** das imagens grandes (tabela colada como imagem, print de notícia, citação escaneada, timbrado). Fica separado para não misturar texto exato com OCR — **para a busca, usar `texto` + `texto_figuras`** |

Resultado da carga completa (06/10/2026, Mac M1 8 GB, 6 processos):

| | |
|---|---|
| PDFs | 916 ok, nenhum erro |
| Páginas | 34.007 |
| Texto nativo | 28.895 páginas |
| OCR | 5.112 páginas, em 114 PDFs; nenhuma `ocr_falhou` |
| Páginas giradas antes do OCR | 44, em 6 PDFs (29 na ADI 4151) |
| Páginas nativas com `texto_figuras` | 2.753, em 231 PDFs |
| Confiança do OCR | mediana 92 (média 88,5); só 33 páginas com texto abaixo de 50 |
| Tamanho | 74,5 milhões de caracteres em `texto` + 1,5 milhão em `texto_figuras` (87 MB de JSONL) |
| Tempo | ~78 min de trabalho (120 min no relógio: o Mac dormiu 42 min com a bateria em 1% — por isso a ADI 4162 aparece com 2.674 s no log) |

- Conferência automática: todo PDF tem o seu JSONL, com todas as páginas na ordem e
  sem `.tmp` sobrando; o número de páginas que foram para o OCR bateu com a previsão
  do levantamento (5.112 × 5.113).
- Conferência manual (amostra sorteada, página × texto): ofício datilografado de 1992
  na ADI 3494 e petição escaneada da ADI 7170 saem fiéis, com acento; o gráfico de uma
  figura da ADI 7775 sai com rótulos e números ("Floresta Primária 301,20 Mha (72,0%)").
- Confiança baixa marca lixo de verdade: das 33 páginas abaixo de 50, 23 são dos anexos
  da ADPF 165 — folhas quase em branco, só com furo de fichário e carimbo de número de
  folha. Outras 58 páginas de OCR saem vazias: folhas em branco (versos e separadores).
- Sobra para a etapa 4: faixas sujas no topo de scans antigos viram lixo curto ("7 TA
  REAR a ARE…"), carimbos laterais embaralhados, recortes de jornal em colunas (ADI 4151,
  página 93) e hifenização de fim de linha.

Levantamento do corpus (06/10/2026), feito antes da carga, página por página, nos
916 PDFs (34.007 páginas; nenhum PDF que não abre):

- Pelo critério de 21/09, **7.845 páginas** (em 183 PDFs, 162 deles inteiros) iriam
  para o OCR — não ~4.400, como dizia a estimativa preliminar.
- Dessas, **2.732 páginas (100 PDFs) não são scan**: são PDFs do Word com o papel
  timbrado do escritório como imagem de fundo do tamanho da página, e o texto nativo
  por cima é perfeito. O critério de 21/09 jogaria esse texto fora para fazer OCR.
  Numa amostra de 175 dessas páginas, fazendo OCR só da imagem de fundo, a mediana
  de palavras do texto nativo encontradas nela foi 0% — a imagem é logotipo e rodapé.
- As outras páginas com texto e imagem de página inteira são scan com OCR antigo:
  579 com o texto invisível (11 PDFs — o caso da ADI 3494) e 103 com o texto
  escondido embaixo da imagem (8 PDFs, ex.: ADPF 429). Nelas, 70–99% das palavras do
  texto nativo estão na imagem.
- Em 12% das páginas "timbradas" da amostra, a imagem de fundo é a petição inteira e
  o "texto nativo" por cima é só o carimbo de assinatura digital ("Assinado por …
  Signature Not Verified" — ex.: ADO 13, ADI 4447, ADPF 219).
- **1.294 páginas nativas (159 PDFs)** têm alguma imagem cobrindo ≥ 15% da página
  sem ser timbrado: tabelas coladas como imagem, prints de notícia, citações
  escaneadas, gráficos, mapas.
- Texto nativo com caractere quebrado (fonte sem mapa de Unicode) é raro: só 4
  páginas, em 2 PDFs (ADI 4612 e ADI 7998), passam de 1% — não justifica critério
  próprio.
- O detector de orientação (OSD) do Tesseract sugeriu girar 151 páginas. Olhando
  uma a uma, ~45 estão de lado de verdade (tabelas deitadas na ADI 4151, recortes de
  jornal na ADI 4164, anexos da ADI 2039 e da ADPF 165); as outras estão em pé —
  inclusive algumas em que o OSD tinha confiança alta.

Decisões (21/09/2026, revistas em 06/10/2026 para a carga de produção):

- **Ferramenta.** PyMuPDF lê o PDF, extrai o texto nativo e renderiza a página em
  imagem; Tesseract 5.5 faz o OCR. Roda local e é gratuito. Nuvem (Document AI,
  Textract) fica como plano B.
  - **Sem Homebrew.** O Tesseract é um programa em C++, e o caminho usual no Mac é
    `brew install`. A saída foi o pacote `tesserocr`, cuja wheel (Mac arm64 e Linux)
    já traz o Tesseract compilado dentro. Os modelos são baixados pelo script na
    primeira rodada, de um commit fixo do repositório `tessdata_best`, e conferidos
    por SHA-256 — a carga é reproduzível e um download pela metade não passa
    despercebido. Ficam em `modelos/tessdata_best/` (fora do git).
  - **Modelo `tessdata_best`** em vez do padrão (06/10). Comparação em 16 páginas
    escaneadas de PDFs diferentes: o `best` acerta o que o padrão erra ("indigenas" →
    "indígenas", "protacolo" → "protocolo", "Guando" → "Quando", "miílitar" →
    "militar", e o cabeçalho "Supremo Tribunal Federal" que o padrão transformava em
    lixo), ao custo de ~40% mais tempo por página (2,96 s × 2,12 s num processo só).
  - Apple Vision (OCR nativo do macOS, via `pyobjc`) foi testado como alternativa:
    ~2x mais rápido e melhor nos carimbos laterais, mas pior num scan com a margem
    cortada (ADPF 165) e preso ao Mac.
- **Escopo.** Todas as páginas de todos os PDFs, anexos inclusos. Como a saída é por
  página, cortar anexo depois é só filtrar — fica para a etapa 4.
- **Quando fazer OCR da página** — decisão por página, com três números:
  1. menos de 50 caracteres de texto nativo → OCR (página em branco ou imagem pura);
  2. a maior imagem cobre ≥ 50% da página **e** menos da metade do texto está visível
     por cima dela → OCR (scan com camada de OCR antigo);
  3. fora disso → texto nativo.
  - "Visível" (06/10) quer dizer desenhado em modo normal (não invisível, não com
    opacidade 0) e **depois** da imagem grande, na ordem em que o PDF desenha a
    página. O PyMuPDF dá essa ordem (`get_bboxlog`) e o modo de cada trecho de texto
    (`get_texttrace`). No corpus quase toda página cai num extremo (≥ 80% ou ≤ 20%
    visível), então o limiar de 0,5 não é sensível.
  - O critério por nome de fonte (`HiddenHorzOCR`) foi testado e descartado: na
    ADI 3494, 33 das 201 páginas do OCR antigo não usam essa fonte.
  - `--forcar-ocr ADI_3494_inicial` força OCR em todas as páginas de um documento,
    para o caso que o critério deixar passar.
- **Figuras (06/10).** Em página nativa, toda imagem que cobre ≥ 15% da página passa
  pelo OCR, e o texto vai para `texto_figuras`. Isso pega as tabelas e os prints e
  também resolve as páginas do tipo ADO 13: o carimbo de assinatura fica em `texto`,
  e a petição, em `texto_figuras`. O OCR é feito na imagem do jeito que ela está
  guardada no PDF (não na página renderizada), para o texto nativo desenhado por cima
  não sair duplicado; imagem em baixa resolução é reescalada para 300 dpi (até 4x).
  O timbrado, que é a mesma imagem em todas as páginas, é lido uma vez só por
  processo.
- **Página de lado (06/10).** Se o OCR de uma página sai com confiança < 70, o script
  pergunta ao OSD se ela está girada; se ele disser que sim, refaz o OCR com a página
  girada e só fica com essa versão se a confiança subir pelo menos 10 pontos. Nas 151
  páginas suspeitas, página de lado de verdade sai de ~40 para ~80, e página em pé
  que o OSD mandaria girar cai de ~85 para ~40 — e fica como estava. O giro aplicado
  vai no campo `giro`.
- **Paralelismo (06/10).** O Tesseract usa um núcleo só. O script abre N processos
  (`--processos`, padrão = núcleos − 2), cada um com o seu Tesseract, e manda as
  páginas de cada PDF para eles; o processo principal junta as páginas na ordem e
  grava. Numa petição escaneada de 103 páginas: 4 processos 113 s, 6 processos 97 s,
  8 processos 91 s — os núcleos de economia do M1 quase não ajudam, e cada processo
  ocupa ~250 MB.
- **Checkpoint e log** no mesmo molde do download: `dados/textos/_estado.json` (salvo
  depois de cada PDF, com a configuração usada — versão do Tesseract, commit dos
  modelos, dpi) e `dados/textos/_log.csv` (processo, arquivo, páginas, nativas, ocr,
  ocr_falhou, giradas, com_figuras, confianca_media, segundos, status, detalhe). O
  JSONL é escrito em `.tmp` e renomeado no fim, para um Ctrl+C não deixar arquivo
  pela metade.
- **Erros.** Um documento com problema não derruba a carga: vai para o log como
  `erro` e é tentado de novo na rodada seguinte. Tesseract que falha numa página não
  perde o documento: a página entra com `"origem": "ocr_falhou"` e texto vazio. Se
  um processo do OCR morrer (falha dentro do Tesseract, falta de memória), o script
  registra o PDF como erro, abre processos novos e segue.
- **Fora do escopo** (etapa 4): cortar anexos, limpar cabeçalho/rodapé/selo do STF,
  juntar a hifenização de fim de linha ("CAR-\nREIRA"), decidir os `_inicial_2`.

Primeira validação, na amostra de 7 PDFs (22/09/2026, modelo padrão, um processo):
o critério acertou os 7 (nativos 100% `nativo`, escaneados 100% `ocr`, ADI 3494
inteira para o OCR); na ADI 3494, "Questlo/hlbrida/flsicas" do OCR antigo viraram
"Questão/híbrida/físicas". Na ADPF 165, 58 páginas saem vazias: são folhas em branco
de verdade (versos e separadores dos anexos), não erro.

O Tesseract imprime `Error in boxClipToRectangle` e `Error in pixScanForForeground`
em algumas páginas. É aviso interno do Leptonica (a biblioteca de imagem dele); não
derruba a página nem muda o resultado.

## Estrutura

```
JurisMatch/
├── README.md                     versão em inglês
├── README.pt-BR.md               este arquivo
├── requirements.txt
├── scripts/
│   ├── baixar_iniciais_stf.py    coleta dos PDFs no portal do STF
│   ├── extrair_texto.py          texto nativo + OCR → JSONL por PDF
│   ├── dividir_trechos.py        texto limpo por página → trechos.jsonl
│   ├── gerar_embeddings.py       trechos → vetores (índice em dados/indice/)
│   └── buscar.py                 consulta → trechos mais parecidos
├── tests/
│   ├── test_extrair_texto.py     testes das funções puras do extrair_texto
│   └── test_dividir_trechos.py   testes do corte em trechos
├── certs/
│   └── stf_intermediario.pem     certificado intermediário (público)
├── modelos/
│   └── tessdata_best/            modelos do Tesseract: por + osd (fora do git; o script baixa)
└── dados/
    ├── incidentes_stf.csv        lista dos 906 processos
    ├── iniciais/
    │   ├── ADC/ ADI/ ADO/ ADPF/  PDFs (fora do git)
    │   ├── _log.csv              registro do download
    │   └── _estado.json          checkpoint
    ├── trechos/trechos.jsonl     um trecho por linha (fora do git)
    ├── indice/                   embeddings.npy + trechos.jsonl + info.json (fora do git)
    ├── textos/
    │   ├── ADC/ ADI/ ADO/ ADPF/  JSONL, um por PDF (fora do git)
    │   ├── _log.csv              registro da extração
    │   ├── _estado.json          checkpoint (e a configuração usada)
    │   └── _run.log              saída da última carga (fora do git)
    └── amostra/                  7 PDFs para teste de OCR (fora do git)
```

Os PDFs (~1 GB) e os JSONL não vão para o repositório — são reconstruídos pelos
scripts. O que fica versionado é a lista de processos, os logs e os checkpoints.

## Como rodar

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# teste rápido com 20 processos
python scripts/baixar_iniciais_stf.py --motor playwright --limite 20

# carga completa (retoma de onde parou)
python scripts/baixar_iniciais_stf.py --motor playwright
```

Opções: `--so-classe ADC`, `--min 4 --max 9` (pausa entre processos, em segundos),
`--refazer` (ignora o checkpoint), `--csv caminho.csv`.

Extração de texto (não precisa instalar nada no sistema: o `tesserocr` já traz o
Tesseract, e os modelos são baixados sozinhos na primeira rodada):

```bash
# teste na amostra (saída separada, pra não misturar com o corpus)
python scripts/extrair_texto.py --pasta dados/amostra/amostra_iniciais --saida dados/amostra/textos

# carga completa (~80 min; retoma de onde parou). Na tomada: o caffeinate impede o Mac
# de dormir por inatividade, mas não com a bateria acabando (aconteceu em 06/10)
caffeinate -i python scripts/extrair_texto.py > dados/textos/_run.log 2>&1

# testes das funções de decisão
pytest
```

Opções: `--so-classe ADO`, `--limite N`, `--refazer` (ignora o checkpoint),
`--forcar-ocr ADI_3494_inicial` (OCR em todas as páginas desse documento),
`--processos 6` (quantos Tesseracts ao mesmo tempo), `--dpi 300`.

Os caminhos são relativos à raiz do projeto, então os scripts rodam de qualquer pasta.

Trechos, embeddings e busca (etapas 4–6):

```bash
python scripts/dividir_trechos.py      # texto limpo → dados/trechos/trechos.jsonl
python scripts/gerar_embeddings.py     # trechos → dados/indice/ (baixa o modelo na 1ª vez, ~1,1 GB)
python scripts/buscar.py "expurgos inflacionários da poupança" -k 5
python scripts/buscar.py "unicidade sindical" --um-por-peticao
```

## Trechos e embeddings (primeira versão, 19/10/2026)

Por enquanto a entrada é o texto **já limpo** da pasta da Ludmila
(`Projeto_final/data/02_processed/clean/<ID>.json`, uma entrada por página, e
`metadata/documentos.csv` com relator, datas e link) — as 6 petições de teste. Os
caminhos ficam em constantes no topo do `dividir_trechos.py` (`ENTRADA`, `METADADOS`).

- **Divisão** (`dividir_trechos.py`): janela de **250 palavras** que anda de 200 em
  200 (50 de sobreposição), sem olhar a estrutura da petição — simples e previsível.
  Cada trecho guarda `page_start`/`page_end` (página da primeira e da última
  palavra), o que deixa a busca dizer "págs. 58–59". Páginas com menos de 50
  caracteres ficam de fora. 250 palavras ≈ 400 tokens, abaixo do limite de 512 do
  encoder.
- **Encoder** (`gerar_embeddings.py`): `intfloat/multilingual-e5-base` pronto, sem
  treino (a linha de base da etapa 5), com os prefixos `passage:` / `query:` que o e5
  pede. Vetores normalizados de 768 dimensões.
- **Índice**: um `embeddings.npy` + cópia do `trechos.jsonl`, e a busca é um produto
  escalar no numpy. Para alguns milhares de trechos isso basta; FAISS só se ficar lento.

Resultado: 6 petições, 2.050 páginas → **3.010 trechos** (mediana de 250 palavras).
Os embeddings levaram **44 min** na CPU de um notebook Windows (≈1,1 trecho/s).
Teste rápido com 6 consultas de resposta conhecida (uma por petição, ex.: "expurgos
inflacionários dos planos econômicos na poupança" → ADPF 165): **6/6** com a petição
certa em 1º lugar, e os 5 primeiros trechos todos da petição certa. É um teste fácil —
são 6 petições de temas bem diferentes —, então ainda não mede o encoder de verdade.

Para o corpus inteiro (~74 milhões de caracteres → ~55 mil trechos) a CPU levaria
~14 h: usar GPU (Colab) ou o `multilingual-e5-small` (`--modelo`), bem mais rápido.

## Próximos passos

### 4. Limpeza e divisão em trechos

- [ ] Juntar `texto` + `texto_figuras` de cada página, tirando de `texto_figuras` o
      timbrado que se repete em todas as páginas do mesmo PDF.
- [ ] Remover ruído repetido: cabeçalhos e rodapés de escritório, numeração de
      folhas, carimbos e o selo lateral de assinatura digital do STF.
- [ ] Dividir em trechos (chunks) respeitando a estrutura da petição — fatos,
      fundamentos, pedido cautelar, pedidos — em vez de cortar por tamanho fixo.
- [ ] Guardar metadados por trecho: processo, classe, relator, data de autuação,
      página.

### 5. Encoder (embeddings) e índice vetorial

O encoder é o modelo que transforma cada trecho em um vetor; textos de sentido
parecido ficam com vetores próximos, e a busca é achar os vetores mais próximos da
consulta. A ideia é ter o **nosso próprio encoder**, em vez de só usar um pronto.
Treinar do zero não é viável — o corpus tem na ordem de 15 milhões de tokens, e um
BERT é treinado com bilhões —, então "nosso" significa adaptar às petições um encoder
já pré-treinado em português.

- [x] Linha de base: um modelo pronto, sem treino (candidatos: `multilingual-e5`,
      `bge-m3`, Legal-BERTimbau).
- [ ] Conjunto de avaliação: 30–50 consultas com resposta conhecida. Sem isso não dá
      para saber se o nosso encoder ficou melhor que o pronto.
- [ ] Adaptação de domínio: continuar o pré-treino (MLM) nas petições, para o modelo
      aprender o vocabulário jurídico. Não precisa de rótulos.
- [ ] Treino contrastivo: ensinar quais textos devem ficar próximos. Precisa de pares,
      que não existem prontos — opções: trechos da mesma petição, petições que atacam
      a mesma lei ou têm o mesmo assunto no portal, ou consultas sintéticas geradas
      por LLM para cada trecho.
- [ ] Comparar o encoder treinado com a linha de base no conjunto de avaliação.
- [ ] Escolher onde indexar: começar simples (FAISS ou `sqlite-vec`, local) e só ir
      para pgvector/Qdrant se precisar.
- [ ] Considerar busca híbrida (BM25 + vetorial): citação de artigo de lei e número
      de processo funcionam melhor por palavra-chave.

### 6. Busca

- [x] CLI ou API simples: entra um texto, saem os N trechos mais parecidos com
      processo, página e link para o portal do STF.
- [ ] Interface web.

### Pendências menores

- [ ] Conferir à mão os 6 processos sem "Petição inicial" — a peça pode existir com
      outro nome na lista.
- [ ] Decidir o que fazer com os 15 PDFs `_inicial_2` / `_inicial_3`: são petições
      diferentes ou a mesma petição dividida em partes?
- [ ] Atualizar `incidentes_stf.csv` periodicamente com processos novos (o script já
      pula o que foi baixado).

## Fonte dos dados

Portal do STF — peças públicas do processo eletrônico
(`portal.stf.jus.br` / `redir.stf.jus.br`).
