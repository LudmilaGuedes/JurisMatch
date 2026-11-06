# -*- coding: utf-8 -*-
"""
Divide o texto limpo de cada peticao em trechos (chunks) para o indice vetorial.

Entrada : data/02_processed/clean/<ID>.json (texto limpo pelo src/ocr_pipeline.py, uma
          entrada por pagina: {"id": ..., "pages": [{"page": 1, "text": "..."}]})
          metadata/documentos.csv (classe, relator, datas, link)
Saida   : data/03_chunks/chunks.jsonl -> um JSON por linha, um por trecho:

    {"chunk_id": "ADI_6002_0007", "document_id": "ADI_6002", "processo": "ADI 6002",
     "classe": "ADI", "relator": "...", "data_autuacao": "2018-09-21",
     "page_start": 12, "page_end": 13, "palavras": 250, "text": "..."}

Como divide: junta as palavras de todas as paginas do documento numa fila so,
lembrando a pagina de cada palavra, e corta janelas de TAMANHO palavras que
andam de PASSO em PASSO (as ultimas TAMANHO - PASSO palavras de um trecho se
repetem no comeco do seguinte, pra uma ideia cortada no meio aparecer inteira
em pelo menos um deles). page_start/page_end sao a pagina da primeira e da
ultima palavra do trecho -- e isso que deixa a busca dizer "paginas 12-13".

Por que 250 palavras: o encoder (multilingual-e5) le no maximo 512 tokens, e em
portugues juridico 250 palavras dao ~400 tokens. Trecho maior seria cortado pelo
modelo sem avisar; trecho bem menor perde o contexto do argumento.

Paginas com menos de MIN_CHARS caracteres (folha em branco, so carimbo) ficam
de fora.

Uso:
    python scripts/dividir_trechos.py
    python scripts/dividir_trechos.py --tamanho 300 --passo 240
"""

import argparse
import csv
import json
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ENTRADA = RAIZ / "data" / "02_processed" / "clean"
METADADOS = RAIZ / "metadata" / "documentos.csv"
SAIDA = RAIZ / "data" / "03_chunks" / "chunks.jsonl"

TAMANHO = 250    # palavras por trecho
PASSO = 200      # quanto a janela anda: sobreposicao de 50 palavras
MIN_CHARS = 50   # pagina com menos texto que isso e ignorada
MIN_PALAVRAS = 30  # sobra final menor que isso e grudada no trecho anterior


def palavras_com_pagina(paginas, min_chars=MIN_CHARS):
    """[(palavra, pagina), ...] de todas as paginas, na ordem."""
    fila = []
    for p in paginas:
        texto = p.get("text") or ""
        if len(texto.strip()) < min_chars:
            continue
        fila.extend((w, p["page"]) for w in texto.split())
    return fila


def janelas(n, tamanho=TAMANHO, passo=PASSO, min_palavras=MIN_PALAVRAS):
    """Intervalos [ini, fim) que cobrem n palavras, com sobreposicao.

    A ultima janela sempre termina em n. Se a sobra depois da penultima for
    pequena demais (< min_palavras palavras novas), ela e absorvida: a
    penultima janela e esticada ate o fim em vez de gerar um trecho minusculo.
    """
    if n == 0:
        return []
    if n <= tamanho:
        return [(0, n)]
    saida = []
    ini = 0
    while ini + tamanho < n:
        saida.append((ini, ini + tamanho))
        ini += passo
    # ultima janela: [ini, n). Palavras novas nela = n - fim da anterior.
    if n - saida[-1][1] < min_palavras:
        saida[-1] = (saida[-1][0], n)
    else:
        saida.append((ini, n))
    return saida


def trechos_do_documento(doc, meta, tamanho=TAMANHO, passo=PASSO):
    fila = palavras_com_pagina(doc["pages"])
    doc_id = doc["id"]
    for i, (ini, fim) in enumerate(janelas(len(fila), tamanho, passo), start=1):
        pedaco = fila[ini:fim]
        yield {
            "chunk_id": f"{doc_id}_{i:04d}",
            "document_id": doc_id,
            "processo": meta.get("processo", doc_id.replace("_", " ")),
            "classe": meta.get("classe", doc_id.split("_")[0]),
            "relator": meta.get("relator", ""),
            "data_autuacao": meta.get("data_autuacao", ""),
            "link": meta.get("link_processo", ""),
            "page_start": pedaco[0][1],
            "page_end": pedaco[-1][1],
            "palavras": len(pedaco),
            "text": " ".join(w for w, _ in pedaco),
        }


def carrega_metadados(caminho):
    if not Path(caminho).exists():
        return {}
    # utf-8-sig: o documentos.csv foi gravado com BOM (pra abrir certo no Excel)
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        return {linha["id"]: linha for linha in csv.DictReader(f)}


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--entrada", default=str(ENTRADA), help="pasta com os <ID>.json limpos")
    p.add_argument("--metadados", default=str(METADADOS))
    p.add_argument("--saida", default=str(SAIDA))
    p.add_argument("--tamanho", type=int, default=TAMANHO)
    p.add_argument("--passo", type=int, default=PASSO)
    args = p.parse_args()
    if not 0 < args.passo <= args.tamanho:
        p.error("--passo precisa ficar entre 1 e --tamanho")

    metas = carrega_metadados(args.metadados)
    arquivos = sorted(Path(args.entrada).glob("*.json"))
    if not arquivos:
        raise SystemExit(f"nenhum .json em {args.entrada}")

    saida = Path(args.saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    tmp = saida.with_suffix(".tmp")
    total = 0
    with open(tmp, "w", encoding="utf-8") as f:
        for arq in arquivos:
            doc = json.loads(arq.read_text(encoding="utf-8"))
            n = 0
            for t in trechos_do_documento(doc, metas.get(doc["id"], {}), args.tamanho, args.passo):
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
                n += 1
            total += n
            print(f"{doc['id']:<12} {len(doc['pages']):>5} paginas -> {n:>5} trechos")
    tmp.replace(saida)  # so troca o arquivo no fim: Ctrl+C nao deixa saida pela metade
    print(f"total: {total} trechos de {len(arquivos)} documentos -> {saida}")


if __name__ == "__main__":
    main()
