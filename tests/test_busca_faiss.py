# -*- coding: utf-8 -*-
"""Testes do Projeto A (src/busca_faiss.py) com vetores sinteticos, sem o modelo."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.busca_faiss import BuscaFAISS, agregar_por_peticao, indexar  # noqa: E402


def normalizar(m):
    return (m / np.linalg.norm(m, axis=1, keepdims=True)).astype(np.float32)


@pytest.fixture
def base(tmp_path):
    """12 peticoes com 1 a 8 trechos cada, vetores aleatorios de 32 dimensoes."""
    rng = np.random.default_rng(0)
    trechos = []
    for d in range(12):
        for c in range(1 + d % 8):
            trechos.append({"chunk_id": f"DOC_{d}_{c}", "document_id": f"DOC_{d}",
                            "page_start": c + 1, "page_end": c + 1, "text": "x"})
    vetores = normalizar(rng.normal(size=(len(trechos), 32)))
    emb, fai = tmp_path / "emb", tmp_path / "faiss"
    emb.mkdir()
    np.save(emb / "embeddings.npy", vetores)
    with open(emb / "chunks.jsonl", "w", encoding="utf-8") as f:
        f.writelines(json.dumps(t) + "\n" for t in trechos)
    (emb / "info.json").write_text(json.dumps(
        {"modelo": "teste", "prefixo_consulta": "query: ", "documentos": 12}), encoding="utf-8")
    busca = BuscaFAISS(pasta_embeddings=emb, pasta_faiss=fai, carregar_modelo=False)
    return busca, vetores, trechos, rng, fai


def referencia_numpy(vetores, trechos, q, k):
    """Python direto: cosseno de todos os trechos e max por peticao."""
    notas = vetores @ q
    melhor = {}
    for t, n in zip(trechos, notas):
        melhor[t["document_id"]] = max(melhor.get(t["document_id"], -9), float(n))
    return sorted(melhor.items(), key=lambda x: -x[1])[:k]


def test_indexar_grava_indice_e_info(base):
    busca, vetores, _, _, fai = base
    info = json.loads((fai / "info.json").read_text(encoding="utf-8"))
    assert (fai / "indice_flatip.faiss").exists()
    assert info["tipo_indice"] == "IndexFlatIP"
    assert info["vetores"] == len(vetores) == busca.indice.ntotal


def test_igual_a_busca_exata_em_numpy(base):
    busca, vetores, trechos, rng, _ = base
    for _ in range(20):
        q = normalizar(rng.normal(size=(1, 32)))[0]
        faiss_res = busca.buscar_ids(q, k=5)
        ref = referencia_numpy(vetores, trechos, q, 5)
        assert [d for d, _ in faiss_res] == [d for d, _ in ref]
        assert np.allclose([n for _, n in faiss_res], [n for _, n in ref], atol=1e-5)


def test_peticoes_distintas_e_ordenadas(base):
    busca, _, _, rng, _ = base
    q = normalizar(rng.normal(size=(1, 32)))[0]
    res = busca.buscar_ids(q, k=10)
    docs = [d for d, _ in res]
    notas = [n for _, n in res]
    assert len(docs) == len(set(docs)) == 10
    assert notas == sorted(notas, reverse=True)


def test_k_maior_que_o_acervo(base):
    busca, _, _, rng, _ = base
    q = normalizar(rng.normal(size=(1, 32)))[0]
    assert len(busca.buscar_ids(q, k=50)) == 12


def test_agregacao_usa_o_max_e_ignora_menos_um():
    docs = ["A", "A", "B", "C"]
    res = agregar_por_peticao([0.9, 0.8, 0.7, 0.1], [1, 0, 2, -1], docs, k=3)
    assert res == [("A", pytest.approx(0.9), 1), ("B", pytest.approx(0.7), 2)]


def test_rejeita_embeddings_nao_normalizados(tmp_path):
    emb = tmp_path / "emb"
    emb.mkdir()
    np.save(emb / "embeddings.npy", np.ones((3, 4), dtype=np.float32))
    (emb / "info.json").write_text(json.dumps({"modelo": "teste"}), encoding="utf-8")
    with pytest.raises(SystemExit):
        indexar(emb, tmp_path / "faiss")
