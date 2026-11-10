# JurisMatch — busca semântica em petições iniciais do STF (NumPy, cosseno direto)

Dada uma consulta em texto (uma tese, um tema, uma pergunta), devolve as **petições
iniciais** de ADI mais parecidas em conteúdo — por significado, não só por palavra-chave —
com a página de onde veio o trecho mais parecido e o link do processo no portal do STF.

Projeto final de Ciências de Dados 5. A recuperação é feita **sem índice vetorial**: os
embeddings de todos os trechos ficam numa matriz em memória e cada consulta é comparada
com todos eles por **similaridade de cosseno direta, em NumPy**.

> Uma versão anterior usava um índice FAISS (`IndexFlatIP`); ela está no histórico do git
> (commit `873705e`). Como aquele índice também fazia busca exata, o top 10 das consultas
> de exemplo é **idêntico** nas duas versões (100 de 100 posições; notas iguais até 10⁻⁶).

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
   │  src/busca_numpy.py  (BuscaNumPy)   matriz em memória, cosseno direto
   ▼
scripts/buscar_numpy.py              consulta → top 10 petições
scripts/rodar_consultas_numpy.py     lista de consultas → resultados.csv + latências (benchmark)
app.py                               front (Streamlit)
```

## Como funciona a busca

- **Embeddings em memória:** `embeddings.npy` (5.107 × 768, `float32`, 15 MB) é carregado
  numa matriz NumPy. Não existe arquivo de índice.
- **Preparo (uma vez):** confere que os vetores estão normalizados e reordena as linhas da
  matriz para que os trechos de cada petição fiquem juntos (~20 ms).
- **Consulta:** texto → vetor com o mesmo modelo, prefixo `query: `, normalizado.
- **Similaridade de cosseno direta:** com vetores de tamanho 1, cosseno = produto interno,
  então a comparação com **todos** os trechos é uma multiplicação `matriz @ vetor`. A busca
  é exata (nenhuma aproximação).
- **Resultado por petição, não por trecho:**
  `nota(petição) = max sobre os trechos da petição de similaridade(trecho, consulta)`,
  calculado com `np.maximum.reduceat`; o top k sai de `np.argpartition` (sem ordenar as
  99 petições inteiras). Cada petição aparece uma vez, com o seu melhor trecho.

### Interface para o benchmark e para o front

```python
import sys; sys.path.insert(0, "<raiz do projeto>")
from src.busca_numpy import BuscaNumPy

busca = BuscaNumPy()          # carrega os embeddings e o modelo

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
python scripts/rodar_consultas_numpy.py metadata/consultas_exemplo.csv --repeticoes 30 --escalas 25,50,99
```

Entrada: CSV `consulta_id,consulta`. `metadata/consultas_exemplo.csv` tem 10 consultas só
para testar o fluxo — **não** é o conjunto de avaliação (gold standard com relevância
0/1/2). Saída em `resultados/numpy/`:

| Arquivo | Conteúdo |
|---|---|
| `resultados.csv` | `consulta_id, consulta, rank, document_id, score, chunk_id, page_start, page_end` — top k petições por consulta (para Precision@5, Recall@10, NDCG@10) |
| `latencias.csv` | `escala_documentos, consulta_id, repeticao, tempo_ms` — uma linha por medição |
| `codificacao.csv` | tempo de texto → vetor de cada consulta (fora do tempo de recuperação) |
| `resumo.json` | média, P50 e P95 por escala; tempo de preparo; memória da matriz e do processo |

Protocolo seguido: mesmas consultas, top 10, 3 buscas de aquecimento por consulta,
repetições em ordem aleatória (semente fixa), embeddings carregados em memória.
`--escalas` monta buscas com 25, 50… petições sorteadas, a partir dos mesmos vetores,
para ver como a latência cresce. Com esta base o máximo é 99 petições.

### Resultados de referência (10/11/2026, notebook, CPU, 16 GB RAM)

Latência de **recuperação** (`buscar_ids`, top 10 petições; 10 consultas de exemplo ×
30 repetições = 300 medições por escala; `resultados/numpy/resumo.json`):

| Petições | Trechos | Preparo | Matriz | Média | P50 | P95 |
|---:|---:|---:|---:|---:|---:|---:|
| 25 | 1.095 | 7,1 ms | 3,2 MB | 0,63 ms | 0,63 ms | 0,83 ms |
| 50 | 2.480 | 18,3 ms | 7,3 MB | 1,15 ms | 1,08 ms | 1,63 ms |
| 99 | 5.107 | 20,2 ms | 15,0 MB | 1,57 ms | 1,51 ms | 2,04 ms |

Transformar a consulta em vetor (modelo e5) levou ~125 ms (P50) nesta rodada — bem mais
que a recuperação, e varia com a carga da máquina. Carregar modelo + embeddings: ~1,8 GB
de memória no processo (quase tudo é o modelo; a matriz ocupa 15 MB).

Conferências: nas 10 consultas de exemplo, a petição esperada aparece no topo — ex.: "piso
salarial nacional da enfermagem" → ADI 7222 (Lei 14.434/2022) em 1º; "improbidade
administrativa e exigência de dolo" → ADI 7236 (Lei 14.230/2021) em 1º. São só testes de
sanidade; a avaliação de qualidade (P@5, R@10, NDCG@10) depende do conjunto de avaliação.

### Filtro "Norma impugnada" no front

Perguntas como "ação contra medida provisória" são, no fundo, um **filtro**, não um tema:
quase toda petição fala em "ação direta", "pedido liminar" e "violação à Constituição", e
a busca semântica não separa bem por isso. Com a frase *"A petição inicial ajuíza ação
direta de inconstitucionalidade com pedido liminar contra medida provisória por violação a
artigos da Constituição?"*, só 1 das 6 ADIs que impugnam medida provisória aparece no top
10; com o filtro "Medida provisória", aparecem as 6.

O tipo de norma vem do campo `legislacao` do `documentos.csv` (`src/normas.py`): lei
federal, lei estadual, lei complementar, emenda constitucional, resolução, decreto,
medida provisória etc. Uma petição pode ter mais de um tipo; lei de conversão de medida
provisória conta como medida provisória. O filtro é aplicado depois do ranqueamento.

> O rótulo `medida_provisoria` da planilha `casos_para_teste.xlsx` **não** indica "ADI
> contra medida provisória": das 35 marcadas com 1, só 3 impugnam uma MP.

## Como rodar

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu   # sem GPU
pip install -r requirements.txt

# já está tudo processado no repositório (os embeddings vão no git)
python scripts/buscar_numpy.py "piso salarial nacional da enfermagem"
python scripts/rodar_consultas_numpy.py metadata/consultas_exemplo.csv --escalas 25,50,99
python -m streamlit run app.py

pytest tests/test_busca_numpy.py tests/test_normas.py tests/test_dividir_trechos.py
```

Para refazer do zero (precisa dos PDFs, que não vão para o git):

```bash
playwright install chromium
python scripts/baixar_iniciais_stf.py --csv metadata/casos_teste.csv --motor playwright --refazer
python scripts/00_registrar_documentos.py
python src/ocr_pipeline.py          # precisa do Tesseract instalado (só para as páginas escaneadas)
python scripts/dividir_trechos.py
python scripts/gerar_embeddings.py  # 1ª vez baixa o modelo (~1,1 GB); na CPU leva horas
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
│   └── 04_embeddings/              embeddings.npy + chunks.jsonl + info.json
├── logs/processing.csv             status e tempo de cada documento por etapa
├── resultados/numpy/               saída do benchmark
├── scripts/
│   ├── baixar_iniciais_stf.py      coleta dos PDFs no portal do STF (Playwright)
│   ├── 00_registrar_documentos.py  ID, cópia do PDF, documentos.csv
│   ├── dividir_trechos.py          texto limpo → trechos
│   ├── gerar_embeddings.py         trechos → vetores
│   ├── buscar_numpy.py             consulta → top k petições
│   ├── rodar_consultas_numpy.py    benchmark: resultados + latências
│   ├── buscar.py                   versão antiga da busca (classe Indice), só como referência
│   └── extrair_texto.py            OCR alternativo do Luigi (tesserocr, Mac/Linux)
├── src/
│   ├── ocr_pipeline.py             extração + OCR + limpeza
│   ├── busca_numpy.py              busca: matriz em memória, cosseno, max por petição (BuscaNumPy)
│   └── normas.py                   tipo de norma impugnada (filtro do front)
├── ocr_padrao.ipynb                notebook que roda e mostra a etapa de OCR
├── app.py + styles.css             front (Streamlit) usando a BuscaNumPy
└── tests/                          pytest
```

**Fora do git** (ver `.gitignore`): PDFs, OCR bruto (`02_processed/ocr/`), `03_chunks/`
(cópia idêntica vai em `04_embeddings/chunks.jsonl`) e pastas locais/históricas.

## Fonte dos dados

Portal do STF — peças públicas do processo eletrônico (`portal.stf.jus.br` / `redir.stf.jus.br`).
