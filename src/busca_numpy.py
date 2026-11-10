# -*- coding: utf-8 -*-
"""
Busca semantica com os embeddings em memoria e similaridade de cosseno direta (NumPy).

Usa os trechos e vetores do pipeline (data/04_embeddings/). Nao ha indice: a
matriz de embeddings (n_trechos x 768) fica na memoria e cada consulta e
comparada com todos os trechos. Como os vetores sao normalizados (tamanho 1),
a similaridade de cosseno e o produto interno, e a comparacao com todos os
trechos e uma unica multiplicacao matriz x vetor:

    similaridades = matriz @ vetor_da_consulta        (n_trechos,)

A busca devolve PETICOES, nao trechos. A nota de cada peticao e a do seu melhor
trecho:

    nota(peticao) = max_{c em peticao} similaridade(c, consulta)

Para isso as linhas da matriz sao reordenadas uma vez, no preparo, para que os
trechos de cada peticao fiquem contiguos; o max por peticao vira um
np.maximum.reduceat e o top k sai de um np.argpartition (sem ordenar tudo).

Duas camadas, para medir tempo separado:
    codificar(texto)          -> vetor da consulta (modelo e5, prefixo "query: ")
    buscar_ids(vetor, k=10)   -> [(document_id, nota), ...]   <- tempo de recuperacao
    buscar(texto, k=10)       -> lista de dicts com processo, relator, paginas,
                                 trecho e link (o que o front precisa mostrar)

Uso:
    from src.busca_numpy import BuscaNumPy
    busca = BuscaNumPy()                      # carrega os embeddings e o modelo
    v = busca.codificar("vedacao de reedicao de medida provisoria")
    busca.buscar_ids(v, k=10)
    busca.buscar("vedacao de reedicao de medida provisoria", k=10)
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
DIR_EMBEDDINGS = RAIZ / "data" / "04_embeddings"
CAMINHO_DOCUMENTOS = RAIZ / "metadata" / "documentos.csv"

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


class BuscaNumPy:
    """Matriz de embeddings + trechos + (opcional) modelo de embeddings, carregados uma vez."""

    def __init__(self, pasta_embeddings=DIR_EMBEDDINGS, carregar_modelo=True,
                 vetores=None, trechos=None):
        self.pasta_embeddings = Path(pasta_embeddings)
        self.info_embeddings = json.loads(
            (self.pasta_embeddings / "info.json").read_text(encoding="utf-8"))
        self.trechos = trechos if trechos is not None else carregar_trechos(self.pasta_embeddings)
        if vetores is None:
            vetores = np.load(self.pasta_embeddings / "embeddings.npy")
        if len(vetores) != len(self.trechos):
            raise SystemExit("embeddings.npy desalinhado com chunks.jsonl: rode "
                             "python scripts/gerar_embeddings.py de novo")

        inicio = time.perf_counter()
        self._preparar(vetores)
        self.tempo_preparo_ms = (time.perf_counter() - inicio) * 1000

        self.documentos = self._carregar_documentos()
        self.modelo = None
        if carregar_modelo:
            from sentence_transformers import SentenceTransformer
            self.modelo = SentenceTransformer(self.info_embeddings["modelo"], device="cpu")

    def _preparar(self, vetores):
        """Confere a normalizacao e agrupa as linhas da matriz por peticao."""
        vetores = np.asarray(vetores, dtype=np.float32)
        if not np.allclose(np.linalg.norm(vetores, axis=1), 1.0, atol=1e-3):
            raise SystemExit("os embeddings nao estao normalizados: o produto interno "
                             "nao seria a similaridade de cosseno")

        self.doc_dos_trechos = [t["document_id"] for t in self.trechos]
        # peticoes na ordem em que aparecem; indice da peticao de cada trecho
        self.ids_documentos = list(dict.fromkeys(self.doc_dos_trechos))
        numero = {d: i for i, d in enumerate(self.ids_documentos)}
        doc_idx = np.array([numero[d] for d in self.doc_dos_trechos], dtype=np.int64)

        # ordem estavel: dentro de cada peticao os trechos mantem a ordem original
        self.ordem = np.argsort(doc_idx, kind="stable")
        self.matriz = np.ascontiguousarray(vetores[self.ordem])
        contagem = np.bincount(doc_idx, minlength=len(self.ids_documentos))
        self.fins = np.cumsum(contagem)
        self.inicios = self.fins - contagem
        self.n_documentos = len(self.ids_documentos)
        self.memoria_matriz_mb = self.matriz.nbytes / 2**20

    @staticmethod
    def _carregar_documentos():
        if not CAMINHO_DOCUMENTOS.exists():
            return {}
        with open(CAMINHO_DOCUMENTOS, encoding="utf-8-sig") as f:
            return {linha["id"]: linha for linha in csv.DictReader(f)}

    # ------------------------------------------------------------ consulta -> vetor
    def codificar(self, consulta):
        if self.modelo is None:
            raise RuntimeError("BuscaNumPy criada com carregar_modelo=False")
        vetor = self.modelo.encode(self.info_embeddings["prefixo_consulta"] + consulta,
                                   normalize_embeddings=True, convert_to_numpy=True)
        return vetor.astype(np.float32)

    # ------------------------------------------------------------ vetor -> peticoes
    def buscar_trechos(self, vetor, k=10):
        """Top k peticoes como [(document_id, nota, posicao_do_melhor_trecho)].

        A posicao e a do trecho em self.trechos (ordem do chunks.jsonl).
        """
        consulta = np.asarray(vetor, dtype=np.float32).reshape(-1)
        k = min(k, self.n_documentos)
        if k <= 0:
            return []
        similaridades = self.matriz @ consulta                       # cosseno de todos os trechos
        notas = np.maximum.reduceat(similaridades, self.inicios)     # max por peticao

        if k < self.n_documentos:
            candidatos = np.argpartition(-notas, k - 1)[:k]
        else:
            candidatos = np.arange(self.n_documentos)
        # nota decrescente; empate pela ordem da peticao (resultado deterministico)
        top = candidatos[np.lexsort((candidatos, -notas[candidatos]))]

        resultado = []
        for g in top:
            ini = self.inicios[g]
            melhor = ini + int(np.argmax(similaridades[ini:self.fins[g]]))
            resultado.append((self.ids_documentos[g], float(notas[g]), int(self.ordem[melhor])))
        return resultado

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
