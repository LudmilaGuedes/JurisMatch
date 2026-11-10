# -*- coding: utf-8 -*-
"""Testes da busca em NumPy (src/busca_numpy.py) com vetores sinteticos, sem o modelo."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.busca_numpy import BuscaNumPy  # noqa: E402


def normalizar(m):
    return (m / np.linalg.norm(m, axis=1, keepdims=True)).astype(np.float32)


def gravar_base(pasta, vetores, trechos):
    pasta.mkdir()
    np.save(pasta / "embeddings.npy", vetores)
    with open(pasta / "chunks.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(t) + "\n" for t in trechos)
    (pasta / "info.json").write_text(json.dumps(
        {"modelo": "teste", "prefixo_consulta": "query: "}), encoding="utf-8")


@pytest.fixture
def base(tmp_path):
    """12 peticoes com 1 a 8 trechos cada, vetores aleatorios de 32 dimensoes.

    Os trechos sao embaralhados para testar peticoes com trechos nao contiguos.
    """
    rng = np.random.default_rng(0)
    trechos = []
    for d in range(12):
        for c in range(1 + d % 8):
            trechos.append({"chunk_id": f"DOC_{d}_{c}", "document_id": f"DOC_{d}",
                            "page_start": c + 1, "page_end": c + 1, "text": "x"})
    rng.shuffle(trechos)
    vetores = normalizar(rng.normal(size=(len(trechos), 32)))
    gravar_base(tmp_path / "emb", vetores, trechos)
    busca = BuscaNumPy(pasta_embeddings=tmp_path / "emb", carregar_modelo=False)
    return busca, vetores, trechos, rng


def referencia_python(vetores, trechos, q, k):
    """Python puro: cosseno de cada trecho e max por peticao, com o melhor trecho."""
    melhor = {}
    for i, t in enumerate(trechos):
        nota = sum(float(a) * float(b) for a, b in zip(vetores[i], q))
        if t["document_id"] not in melhor or nota > melhor[t["document_id"]][0]:
            melhor[t["document_id"]] = (nota, i)
    ordem = sorted(melhor.items(), key=lambda x: -x[1][0])[:k]
    return [(d, n, i) for d, (n, i) in ordem]


def test_igual_a_referencia_em_python(base):
    busca, vetores, trechos, rng = base
    for _ in range(20):
        q = normalizar(rng.normal(size=(1, 32)))[0]
        res = busca.buscar_trechos(q, k=5)
        ref = referencia_python(vetores, trechos, q, 5)
        assert [d for d, _, _ in res] == [d for d, _, _ in ref]
        assert np.allclose([n for _, n, _ in res], [n for _, n, _ in ref], atol=1e-5)
        # a posicao devolvida e a do melhor trecho, na ordem original do chunks.jsonl
        assert [p for _, _, p in res] == [p for _, _, p in ref]


def test_peticoes_distintas_e_ordenadas(base):
    busca, _, _, rng = base
    q = normalizar(rng.normal(size=(1, 32)))[0]
    res = busca.buscar_ids(q, k=10)
    docs = [d for d, _ in res]
    notas = [n for _, n in res]
    assert len(docs) == len(set(docs)) == 10
    assert notas == sorted(notas, reverse=True)


def test_k_maior_que_o_acervo(base):
    busca, _, _, rng = base
    q = normalizar(rng.normal(size=(1, 32)))[0]
    assert len(busca.buscar_ids(q, k=50)) == 12
    assert busca.buscar_ids(q, k=0) == []


def test_nota_da_peticao_e_o_max_dos_trechos(tmp_path):
    trechos = [{"chunk_id": c, "document_id": d, "page_start": 1, "page_end": 1, "text": "x"}
               for c, d in [("A0", "A"), ("B0", "B"), ("A1", "A"), ("C0", "C")]]
    q = np.array([1.0, 0.0], dtype=np.float32)
    angulos = [0.8, 0.5, 0.1, 1.4]                    # cosseno com q: 0,70 0,88 0,99 0,17
    vetores = np.array([[np.cos(a), np.sin(a)] for a in angulos], dtype=np.float32)
    gravar_base(tmp_path / "emb", vetores, trechos)
    busca = BuscaNumPy(pasta_embeddings=tmp_path / "emb", carregar_modelo=False)
    res = busca.buscar_trechos(q, k=3)
    assert [(d, p) for d, _, p in res] == [("A", 2), ("B", 1), ("C", 3)]
    assert res[0][1] == pytest.approx(np.cos(0.1))


def test_rejeita_embeddings_nao_normalizados(tmp_path):
    trechos = [{"chunk_id": str(i), "document_id": "A", "page_start": 1, "page_end": 1,
                "text": "x"} for i in range(3)]
    gravar_base(tmp_path / "emb", np.ones((3, 4), dtype=np.float32), trechos)
    with pytest.raises(SystemExit):
        BuscaNumPy(pasta_embeddings=tmp_path / "emb", carregar_modelo=False)


def test_rejeita_vetores_desalinhados(tmp_path):
    trechos = [{"chunk_id": "0", "document_id": "A", "page_start": 1, "page_end": 1, "text": "x"}]
    gravar_base(tmp_path / "emb", normalizar(np.ones((2, 4))), trechos)
    with pytest.raises(SystemExit):
        BuscaNumPy(pasta_embeddings=tmp_path / "emb", carregar_modelo=False)
