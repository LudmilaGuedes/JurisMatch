# -*- coding: utf-8 -*-
"""
Busca semantica: entra um texto, saem os trechos de peticao mais parecidos.

Le o indice gravado pelo gerar_embeddings.py (data/04_embeddings/), transforma a
consulta em vetor com o mesmo modelo (prefixo "query: ") e ordena os trechos
pelo produto escalar -- que, com vetores normalizados, e a similaridade de
cosseno (1 = mesmo sentido, perto de 0 = nada a ver).

Cada resultado mostra o processo, as paginas e o comeco do trecho, pra dar
pra conferir no PDF de onde veio.

Uso:
    python scripts/buscar.py "inconstitucionalidade de lei estadual sobre licenciamento ambiental"
    python scripts/buscar.py "expurgos inflacionarios da poupanca" -k 10
    python scripts/buscar.py "unicidade sindical" --um-por-peticao
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

RAIZ = Path(__file__).resolve().parent.parent
INDICE = RAIZ / "data" / "04_embeddings"


class Indice:
    """Vetores + trechos + modelo, carregados uma vez (serve pro app tambem)."""

    def __init__(self, pasta=INDICE):
        pasta = Path(pasta)
        self.info = json.loads((pasta / "info.json").read_text(encoding="utf-8"))
        self.vetores = np.load(pasta / "embeddings.npy")
        with open(pasta / "chunks.jsonl", encoding="utf-8") as f:
            self.trechos = [json.loads(linha) for linha in f if linha.strip()]
        if len(self.trechos) != len(self.vetores):
            raise SystemExit("indice desalinhado: rode o gerar_embeddings.py de novo")
        self.modelo = SentenceTransformer(self.info["modelo"], device="cpu")

    def buscar(self, consulta, k=5, um_por_peticao=False):
        q = self.modelo.encode(self.info["prefixo_consulta"] + consulta,
                               normalize_embeddings=True)
        notas = self.vetores @ q
        resultados, vistos = [], set()
        for i in np.argsort(-notas):
            t = self.trechos[i]
            if um_por_peticao:
                if t["document_id"] in vistos:
                    continue
                vistos.add(t["document_id"])
            resultados.append({**t, "similaridade": float(notas[i])})
            if len(resultados) == k:
                break
        return resultados


def paginas(t):
    if t["page_start"] == t["page_end"]:
        return f"pag. {t['page_start']}"
    return f"pags. {t['page_start']}-{t['page_end']}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # terminal do Windows nao e utf-8
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("consulta")
    p.add_argument("-k", type=int, default=5, help="quantos resultados")
    p.add_argument("--um-por-peticao", action="store_true",
                   help="so o melhor trecho de cada peticao")
    p.add_argument("--indice", default=str(INDICE))
    args = p.parse_args()

    indice = Indice(args.indice)
    for n, r in enumerate(indice.buscar(args.consulta, args.k, args.um_por_peticao), start=1):
        print(f"\n{n}. {r['processo']} | {paginas(r)} | similaridade {r['similaridade']:.3f}"
              f" | {r['chunk_id']}")
        if r.get("link"):
            print(f"   {r['link']}")
        print(f"   {r['text'][:400]}...")


if __name__ == "__main__":
    main()
