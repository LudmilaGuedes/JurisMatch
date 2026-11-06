# JurisMatch — Projeto A: busca semântica com FAISS em petições iniciais do STF

Dada uma consulta em texto (uma tese, um tema, uma pergunta), devolve as **petições
iniciais** de ADI mais parecidas em conteúdo — por significado, não só por palavra-chave —
com a página de onde veio o trecho mais parecido e o link do processo no portal do STF.

Projeto final de Ciências de Dados 5. O grupo compara duas abordagens de recuperação com
os **mesmos** textos, trechos e embeddings:

- **Projeto A (este repositório): índice FAISS** `IndexFlatIP`.
- Projeto B: comparação direta de vetores em Python (feito por outra pessoa do grupo).

O front e o artigo comparando os dois também ficam com outras pessoas; este repositório
entrega tudo o que eles precisam do Projeto A (dados processados, índice, função de busca
padronizada e script de benchmark).

## Base: 99 ADIs de `casos_para_teste.xlsx`

A base é a aba **Sheet2** de `data/00_planilhas/casos_para_teste.xlsx`: **100 ADIs**, com o
rótulo **"Indicador Medida Provisória"** (1 = a ADI questiona uma medida provisória).
A lista está em `metadata/casos_teste.csv`.

| | |
|---|---|
| ADIs na lista | 100 (35 sobre medida provisória, 65 não) |
| Petições iniciais baixadas | **99** — a ADI 7548 não tem nenhuma peça "Petição inicial" no portal |
| Duas iniciais no mesmo processo | ADI 7708: entra a de 2024 (original); a de 2025 fica registrada como descartada |
| Páginas | 3.448 (7 a 151 por petição; média 35) |
| Extração | 93 com texto digital, 4 mistas, 2 escaneadas (OCR) — 33 páginas com OCR |
| Trechos | **5.107** (250 palavras, passo 200) |
| Embeddings | `intfloat/multilingual-e5-base`, 768 dimensões, normalizados |

## Pipeline

Cada petição tem um ID estável `<CLASSE>_<NÚMERO>` (ex.: `ADI_7708`), mantido em todas as
etapas: **documento → página → trecho → resultado da busca**.

```
metadata/casos_teste.csv  (100 ADIs da Sheet2)
   │  scripts/baixar_iniciais_stf.py --csv metadata/casos_teste.csv --motor playwright
   ▼
data/01_raw/iniciais_stf/ADI/*.pdf
   │  scripts/00_registrar_documentos.py
   ▼
data/01_raw/pdf/<ID>.pdf  +  metadata/documentos.csv  (metadados + rótulo medida_provisoria)
   │  src/ocr_pipeline.py            texto nativo; OCR (Tesseract) só onde precisa; limpeza
   ▼
data/02_processed/clean/<ID>.json    texto limpo, por página
   │  scripts/dividir_trechos.py     250 palavras, passo 200, page_start/page_end
   ▼
data/03_chunks/chunks.jsonl
   │  scripts/gerar_embeddings.py    multilingual-e5-base, prefixo "passage: "
   ▼
data/04_embeddings/                  embeddings.npy + chunks.jsonl (mesma ordem) + info.json
   │  scripts/indexar_faiss.py       ← PROJETO A
   ▼
data/05_faiss/                       indice_flatip.faiss + info.json (tempo, memória)
   │  src/busca_faiss.py  (BuscaFAISS)
   ▼
scripts/buscar_faiss.py              consulta → top 10 petições
scripts/rodar_consultas_faiss.py     lista de consultas → resultados.csv + latências (benchmark)
```

## Projeto A — como funciona a busca

- **Índice:** `faiss.IndexFlatIP` com os vetores de `data/04_embeddings/embeddings.npy`.
  Como os vetores são normalizados, produto interno = **similaridade de cosseno**, e o
  `IndexFlatIP` faz **busca exata** — o ranking é o mesmo de uma comparação direta no NumPy
  (isso é testado em `tests/test_busca_faiss.py`). A diferença para o Projeto B, portanto,
  deve aparecer em **tempo e memória**, não em qualidade.
- **Consulta:** texto → vetor com o mesmo modelo, prefixo `query: `, normalizado.
- **Resultado por petição, não por trecho:**
  `nota(petição) = max sobre os trechos da petição de similaridade(trecho, consulta)`.
  O índice é consultado com trechos candidatos suficientes (começa com `max(50, 20·k)`
  e aumenta se precisar) para fechar **k petições distintas**.

### Interface para o benchmark e para o front

```python
import sys; sys.path.insert(0, "<raiz do projeto>")
from src.busca_faiss import BuscaFAISS

busca = BuscaFAISS()          # carrega o índice (ou cria, se data/05_faiss/ não existir) e o modelo

v = busca.codificar("vedação de reedição de medida provisória")   # texto -> vetor (768,)
busca.buscar_ids(v, k=10)     # [(document_id, nota), ...]  ← só recuperação: é o que se cronometra

busca.buscar("vedação de reedição de medida provisória", k=10)
# [{rank, document_id, score, chunk_id, page_start, page_end, trecho, processo, relator,
#   data_autuacao, assuntos, legislacao, medida_provisoria, link_processo, paginas}, ...]
```

`buscar_ids` recebe o embedding já pronto e devolve os IDs, como pede o protocolo do
benchmark — o tempo do modelo de embeddings fica fora do tempo de recuperação.

### Benchmark

```bash
python scripts/rodar_consultas_faiss.py metadata/consultas_exemplo.csv --repeticoes 30 --escalas 25,50,99
```

Entrada: CSV `consulta_id,consulta`. `metadata/consultas_exemplo.csv` tem 10 consultas só
para testar o fluxo — **não** é o conjunto de avaliação (gold standard com relevância
0/1/2), que é feito por quem escreve a comparação. Saída em `resultados/projeto_a/`:

| Arquivo | Conteúdo |
|---|---|
| `resultados.csv` | `consulta_id, consulta, rank, document_id, score, chunk_id, page_start, page_end` — top k petições por consulta (para Precision@5, Recall@10, NDCG@10) |
| `latencias.csv` | `escala_documentos, consulta_id, repeticao, tempo_ms` — uma linha por medição |
| `codificacao.csv` | tempo de texto → vetor de cada consulta (parte do tempo total, igual nos dois projetos) |
| `resumo.json` | média, P50 e P95 por escala; tempo de indexação; memória do índice e do processo |

Protocolo seguido: mesmas consultas, top 10, 3 buscas de aquecimento por consulta,
repetições em ordem aleatória (semente fixa), índice carregado em memória.
`--escalas` monta índices com 25, 50… petições sorteadas, a partir dos mesmos vetores,
para ver como a latência cresce. Com esta base o máximo é 99 petições.

### Resultados de referência (06/11/2026, notebook, CPU, 16 GB RAM)

Índice (`data/05_faiss/info.json`): 5.107 vetores × 768, **indexação em 6 ms**, índice com
**15 MB** na memória e em disco. Embeddings dos trechos: ~2 h na CPU (`e5-base`), em três
execuções retomadas pelo checkpoint (o `segundos` do `info.json` conta só a última).

Latência de **recuperação** (`buscar_ids`, top 10 petições; 10 consultas de exemplo ×
30 repetições = 300 medições por escala; `resultados/projeto_a/resumo.json`):

| Petições | Trechos | Média | P50 | P95 |
|---:|---:|---:|---:|---:|
| 25 | 1.095 | 0,24 ms | 0,25 ms | 0,27 ms |
| 50 | 2.480 | 0,41 ms | 0,29 ms | 0,81 ms |
| 99 | 5.107 | 1,11 ms | 1,04 ms | 1,62 ms |

Transformar a consulta em vetor (modelo e5) leva ~42 ms (P50), muito mais que a
recuperação — essa parte é igual nos dois projetos. Carregar modelo + índice: ~1,8 GB de
memória no processo (quase tudo é o modelo).

Conferências: em 50 consultas sobre os vetores reais, o top 10 do FAISS foi **idêntico**
ao da busca exata em NumPy (50/50). Nas consultas de exemplo, a petição esperada aparece
no topo — ex.: "piso salarial nacional da enfermagem" → ADI 7222 (Lei 14.434/2022) em 1º;
"improbidade administrativa e exigência de dolo" → ADI 7236 (Lei 14.230/2021) em 1º. São
só testes de sanidade; a avaliação de qualidade (P@5, R@10, NDCG@10) depende do conjunto
de avaliação do grupo.

## Como rodar

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu   # sem GPU
pip install -r requirements.txt

# já está tudo processado no repositório; só o índice é recriado (leva milissegundos)
python scripts/indexar_faiss.py
python scripts/buscar_faiss.py "piso salarial nacional da enfermagem"
python scripts/rodar_consultas_faiss.py metadata/consultas_exemplo.csv --escalas 25,50,99

pytest tests/test_busca_faiss.py tests/test_dividir_trechos.py
```

Para refazer do zero (precisa dos PDFs, que não vão para o git):

```bash
playwright install chromium
python scripts/baixar_iniciais_stf.py --csv metadata/casos_teste.csv --motor playwright --refazer
python scripts/00_registrar_documentos.py
python src/ocr_pipeline.py          # precisa do Tesseract instalado (só para as páginas escaneadas)
python scripts/dividir_trechos.py
python scripts/gerar_embeddings.py  # 1ª vez baixa o modelo (~1,1 GB); na CPU leva horas
python scripts/indexar_faiss.py
```

## Estrutura

```
Projeto_final/
├── README.md
├── docs/
│   ├── ocr_e_limpeza.md            detalhes e decisões do OCR e da limpeza
│   └── jurismatch_luigi.md         README original do Luigi: coleta e decisões
├── requirements.txt / pytest.ini
├── certs/stf_intermediario.pem     certificado público usado pelo download
├── metadata/
│   ├── casos_teste.csv             as 100 ADIs da base + rótulo medida_provisoria
│   ├── documentos.csv              uma petição por linha: processo, relator, assuntos,
│   │                               legislação, medida_provisoria, páginas, método de extração
│   ├── consultas_exemplo.csv       10 consultas para testar o benchmark
│   └── incidentes_stf.csv          os 906 processos com o incidente (usado pelo download)
├── data/
│   ├── 00_planilhas/               planilhas originais (.xlsx)
│   ├── 01_raw/iniciais_stf/_log.csv  log do download (PDFs fora do git)
│   ├── 02_processed/clean/<ID>.json  texto limpo por página
│   ├── 04_embeddings/              embeddings.npy + chunks.jsonl + info.json
│   └── 05_faiss/info.json          tempo de indexação e memória (o .faiss é recriado)
├── logs/processing.csv             status e tempo de cada documento por etapa
├── resultados/projeto_a/           saída do benchmark
├── scripts/
│   ├── baixar_iniciais_stf.py      coleta dos PDFs no portal do STF (Playwright)
│   ├── 00_registrar_documentos.py  ID, cópia do PDF, documentos.csv
│   ├── dividir_trechos.py          texto limpo → trechos
│   ├── gerar_embeddings.py         trechos → vetores
│   ├── indexar_faiss.py            vetores → índice FAISS            (Projeto A)
│   ├── buscar_faiss.py             consulta → top k petições          (Projeto A)
│   ├── rodar_consultas_faiss.py    benchmark: resultados + latências  (Projeto A)
│   ├── buscar.py                   busca exata em NumPy, só como referência
│   └── extrair_texto.py            OCR alternativo do Luigi (tesserocr, Mac/Linux)
├── src/
│   ├── ocr_pipeline.py             extração + OCR + limpeza
│   └── busca_faiss.py              Projeto A: índice, agregação por petição, BuscaFAISS
├── ocr_padrao.ipynb                notebook que roda e mostra a etapa de OCR
├── app.py                          protótipo de front (Streamlit), só como ponto de partida
└── tests/                          pytest
```

**Fora do git** (ver `.gitignore`): PDFs, OCR bruto (`02_processed/ocr/`), `03_chunks/`
(cópia idêntica vai em `04_embeddings/chunks.jsonl`), o arquivo `.faiss` (recriado em
milissegundos a partir dos embeddings) e pastas locais/históricas.

## Fonte dos dados

Portal do STF — peças públicas do processo eletrônico (`portal.stf.jus.br` / `redir.stf.jus.br`).
