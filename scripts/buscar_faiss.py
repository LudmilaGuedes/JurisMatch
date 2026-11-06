# -*- coding: utf-8 -*-
"""
Projeto A - busca por linha de comando: consulta -> top k peticoes (FAISS).

Cada peticao aparece uma vez, com a nota do seu melhor trecho, as paginas desse
trecho e o link do processo no portal do STF.

Uso:
    python scripts/buscar_faiss.py "reedicao de medida provisoria na mesma sessao legislativa"
    python scripts/buscar_faiss.py "piso salarial da enfermagem" -k 5
    python scripts/buscar_faiss.py "contribuicao sindical" --json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.busca_faiss import BuscaFAISS  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # terminal do Windows nao e utf-8
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("consulta")
    p.add_argument("-k", type=int, default=10, help="quantas peticoes")
    p.add_argument("--json", action="store_true", help="saida em JSON")
    args = p.parse_args()

    resultados = BuscaFAISS().buscar(args.consulta, k=args.k)
    if args.json:
        print(json.dumps(resultados, indent=2, ensure_ascii=False))
        return
    for r in resultados:
        pags = (f"pag. {r['page_start']}" if r["page_start"] == r["page_end"]
                else f"pags. {r['page_start']}-{r['page_end']}")
        print(f"\n{r['rank']}. {r['processo']} | score {r['score']:.4f} | {pags} | {r['relator']}")
        print(f"   {r['link_processo']}")
        print(f"   {r['trecho'][:300]}...")


if __name__ == "__main__":
    main()
