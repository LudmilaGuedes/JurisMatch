# -*- coding: utf-8 -*-
"""
Projeto A - busca semantica com indice FAISS.

Usa os mesmos trechos e vetores do pipeline (data/04_embeddings/) e guarda um
indice FAISS IndexFlatIP em data/05_faiss/. Com vetores normalizados, o produto
interno e a similaridade de cosseno, e o IndexFlatIP faz busca exata: o
ranqueamento tem que ser o mesmo de uma comparacao direta no NumPy.

A busca devolve PETICOES, nao trechos. A nota de cada peticao e a do seu melhor
trecho:

    nota(peticao) = max_{c em peticao} similaridade(c, consulta)

e o indice e consultado com trechos candidatos suficientes para fechar k
peticoes distintas (a mesma peticao nao ocupa varias posicoes).

Duas camadas, para medir tempo separado:
    codificar(texto)          -> vetor da consulta (modelo e5, prefixo "query: ")
    buscar_ids(vetor, k=10)   -> [(document_id, nota), ...]   <- tempo de recuperacao
    buscar(texto, k=10)       -> lista de dicts com processo, relator, paginas,
                                 trecho e link (o que um front precisa mostrar)

Uso:
    from src.busca_faiss import BuscaFAISS
    busca = BuscaFAISS()                      # carrega (ou cria) o indice e o modelo
    v = busca.codificar("vedacao de reedicao de medida provisoria")
    busca.buscar_ids(v, k=10)
    busca.buscar("vedacao de reedicao de medida provisoria", k=10)
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path

import faiss
import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
DIR_EMBEDDINGS = RAIZ / "data" / "04_embeddings"
DIR_FAISS = RAIZ / "data" / "05_faiss"
CAMINHO_DOCUMENTOS = RAIZ / "metadata" / "documentos.csv"
ARQUIVO_INDICE = "indice_flatip.faiss"

# colunas do documentos.csv que acompanham cada resultado
COLUNAS_DOCUMENTO = ["processo", "relator", "data_autuacao", "assuntos",
                     "legislacao", "medida_provisoria", "link_processo", "paginas"]


def memoria_processo_mb():
    """Memoria residente do processo (RSS), em MB; None se o psutil nao estiver instalado."""
    try:
        import psutil
    except ImportError:
        return None
    return psutil.Process().memory_info().rss / 2**20


def carregar_trechos(pasta=DIR_EMBEDDINGS):
    with open(Path(pasta) / "chunks.jsonl", encoding="utf-8") as f:
        return [json.loads(linha) for linha in f if linha.strip()]


def construir_indice(vetores):
    """IndexFlatIP (busca exata por produto interno) com os vetores dados."""
    vetores = np.ascontiguousarray(vetores, dtype=np.float32)
    indice = faiss.IndexFlatIP(vetores.shape[1])
    indice.add(vetores)
    return indice


def indexar(pasta_embeddings=DIR_EMBEDDINGS, pasta_faiss=DIR_FAISS):
    """Cria o indice a partir de embeddings.npy, grava em disco e registra tempo e memoria."""
    pasta_embeddings, pasta_faiss = Path(pasta_embeddings), Path(pasta_faiss)
    info_emb = json.loads((pasta_embeddings / "info.json").read_text(encoding="utf-8"))
    vetores = np.load(pasta_embeddings / "embeddings.npy")

    normas = np.linalg.norm(vetores, axis=1)
    if not np.allclose(normas, 1.0, atol=1e-3):
        raise SystemExit("os embeddings nao estao normalizados: o IndexFlatIP nao seria cosseno")

    mem_antes = memoria_processo_mb()
    inicio = time.perf_counter()
    indice = construir_indice(vetores)
    segundos = time.perf_counter() - inicio
    mem_depois = memoria_processo_mb()

    pasta_faiss.mkdir(parents=True, exist_ok=True)
    faiss.write_index(indice, str(pasta_faiss / ARQUIVO_INDICE))
    info = {
        "tipo_indice": "IndexFlatIP",
        "busca": "exata (produto interno = cosseno, vetores normalizados)",
        "faiss_versao": faiss.__version__,
        "vetores": int(indice.ntotal),
        "dimensao": int(indice.d),
        "documentos": info_emb.get("documentos"),
        "modelo_embeddings": info_emb["modelo"],
        "embeddings_gerados_em": info_emb.get("gerado_em"),
        "tempo_indexacao_ms": round(segundos * 1000, 3),
        "memoria_indice_mb": round(indice.ntotal * indice.d * 4 / 2**20, 2),
        "memoria_processo_delta_mb": (round(mem_depois - mem_antes, 2)
                                      if mem_antes is not None else None),
        "tamanho_arquivo_mb": round((pasta_faiss / ARQUIVO_INDICE).stat().st_size / 2**20, 2),
        "agregacao": "max por peticao",
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
    }
    (pasta_faiss / "info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False),
                                           encoding="utf-8")
    return indice, info


def agregar_por_peticao(notas, posicoes, doc_dos_trechos, k):
    """Top k peticoes distintas a partir de trechos ja ordenados por nota (decrescente).

    Como os trechos chegam em ordem, o primeiro trecho visto de cada peticao e o
    maior -- a nota da peticao e o max. Devolve [(document_id, nota, posicao_trecho)].
    """
    resultado, vistos = [], set()
    for nota, pos in zip(notas, posicoes):
        if pos < 0:          # o FAISS completa com -1 quando faltam vetores
            continue
        doc = doc_dos_trechos[pos]
        if doc in vistos:
            continue
        vistos.add(doc)
        resultado.append((doc, float(nota), int(pos)))
        if len(resultado) == k:
            break
    return resultado


class BuscaFAISS:
    """Indice FAISS + trechos + (opcional) modelo de embeddings, carregados uma vez."""

    def __init__(self, pasta_embeddings=DIR_EMBEDDINGS, pasta_faiss=DIR_FAISS,
                 carregar_modelo=True, indice=None, trechos=None):
        self.pasta_embeddings = Path(pasta_embeddings)
        self.info_embeddings = json.loads(
            (self.pasta_embeddings / "info.json").read_text(encoding="utf-8"))
        self.trechos = trechos if trechos is not None else carregar_trechos(self.pasta_embeddings)

        if indice is None:
            arquivo = Path(pasta_faiss) / ARQUIVO_INDICE
            if arquivo.exists():
                indice = faiss.read_index(str(arquivo))
            else:
                indice, _ = indexar(self.pasta_embeddings, pasta_faiss)
        self.indice = indice
        if self.indice.ntotal != len(self.trechos):
            raise SystemExit("indice FAISS desalinhado com os trechos: rode "
                             "python scripts/indexar_faiss.py de novo")

        self.doc_dos_trechos = [t["document_id"] for t in self.trechos]
        self.n_documentos = len(set(self.doc_dos_trechos))
        self.documentos = self._carregar_documentos()
        self.modelo = None
        if carregar_modelo:
            from sentence_transformers import SentenceTransformer
            self.modelo = SentenceTransformer(self.info_embeddings["modelo"], device="cpu")

    @staticmethod
    def _carregar_documentos():
        if not CAMINHO_DOCUMENTOS.exists():
            return {}
        with open(CAMINHO_DOCUMENTOS, encoding="utf-8-sig") as f:
            return {linha["id"]: linha for linha in csv.DictReader(f)}

    # ------------------------------------------------------------ consulta -> vetor
    def codificar(self, consulta):
        if self.modelo is None:
            raise RuntimeError("BuscaFAISS criada com carregar_modelo=False")
        vetor = self.modelo.encode(self.info_embeddings["prefixo_consulta"] + consulta,
                                   normalize_embeddings=True, convert_to_numpy=True)
        return vetor.astype(np.float32)

    # ------------------------------------------------------------ vetor -> peticoes
    def buscar_trechos(self, vetor, k=10):
        """Top k peticoes como [(document_id, nota, posicao_do_melhor_trecho)]."""
        consulta = np.ascontiguousarray(np.asarray(vetor, dtype=np.float32).reshape(1, -1))
        k = min(k, self.n_documentos)
        total = self.indice.ntotal
        candidatos = min(total, max(50, 20 * k))
        while True:
            notas, posicoes = self.indice.search(consulta, candidatos)
            resultado = agregar_por_peticao(notas[0], posicoes[0], self.doc_dos_trechos, k)
            if len(resultado) >= k or candidatos >= total:
                return resultado
            candidatos = min(total, candidatos * 4)

    def buscar_ids(self, vetor, k=10):
        """Funcao do benchmark: embedding da consulta -> [(document_id, nota)] (top k peticoes)."""
        return [(doc, nota) for doc, nota, _ in self.buscar_trechos(vetor, k)]

    # ------------------------------------------------------------ texto -> resultados
    def buscar(self, consulta, k=10):
        """Consulta em texto -> top k peticoes com o melhor trecho e os metadados."""
        resultados = []
        for rank, (doc, nota, pos) in enumerate(self.buscar_trechos(self.codificar(consulta), k), 1):
            t = self.trechos[pos]
            meta = self.documentos.get(doc, {})
            resultados.append({
                "rank": rank,
                "document_id": doc,
                "score": nota,
                "chunk_id": t["chunk_id"],
                "page_start": t["page_start"],
                "page_end": t["page_end"],
                "trecho": t["text"],
                **{c: meta.get(c, "") for c in COLUNAS_DOCUMENTO},
                "link_processo": meta.get("link_processo") or t.get("link", ""),
            })
        return resultados
