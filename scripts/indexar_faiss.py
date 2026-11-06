# -*- coding: utf-8 -*-
"""
Projeto A - cria o indice FAISS (IndexFlatIP) a partir dos embeddings.

Entrada : data/04_embeddings/embeddings.npy (+ info.json, chunks.jsonl)
Saida   : data/05_faiss/indice_flatip.faiss
          data/05_faiss/info.json  -> vetores, dimensao, tempo de indexacao, memoria

Uso:
    python scripts/indexar_faiss.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.busca_faiss import DIR_EMBEDDINGS, DIR_FAISS, indexar  # noqa: E402


def main():
    _, info = indexar(DIR_EMBEDDINGS, DIR_FAISS)
    print(json.dumps(info, indent=2, ensure_ascii=False))
    print(f"\nindice em {DIR_FAISS}")


if __name__ == "__main__":
    main()
