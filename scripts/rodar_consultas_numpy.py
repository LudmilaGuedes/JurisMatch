# -*- coding: utf-8 -*-
"""
Roda uma lista de consultas na busca em NumPy (cosseno direto) e grava resultados
e tempos no formato do benchmark (mesmas consultas, top 10, 30 repeticoes, aquecimento).

Entrada : CSV com as colunas  consulta_id,consulta
          (ex.: metadata/consultas_exemplo.csv; o conjunto de avaliacao oficial,
          com a relevancia 0/1/2, e feito por quem escreve a comparacao)
Saida   : resultados/numpy/
          resultados.csv   consulta_id, consulta, rank, document_id, score, chunk_id,
                           page_start, page_end   (top k peticoes por consulta)
          latencias.csv    escala_documentos, consulta_id, repeticao, tempo_ms
                           (so a recuperacao: o vetor da consulta ja esta pronto)
          codificacao.csv  consulta_id, tempo_ms (texto -> vetor, uma vez por consulta)
          resumo.json      media / P50 / P95 por escala, tempo de preparo, memoria

Tempo de recuperacao = matriz @ vetor + max por peticao + top k, ate ter os k IDs.
O tempo do modelo de embeddings fica de fora (vai em codificacao.csv).
As repeticoes rodam em ordem aleatoria, depois de 3 buscas de aquecimento.

Preparo (o equivalente a "indexar"): conferir a normalizacao e reordenar as linhas
da matriz por peticao. Nao ha arquivo de indice: a matriz e o proprio embeddings.npy.

Escalabilidade (--escalas): monta buscas com N peticoes sorteadas (semente fixa)
a partir dos mesmos vetores e mede a latencia em cada uma.

Uso:
    python scripts/rodar_consultas_numpy.py metadata/consultas_exemplo.csv
    python scripts/rodar_consultas_numpy.py consultas.csv -k 10 --repeticoes 30 --escalas 25,50,99
"""

import argparse
import csv
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.busca_numpy import (DIR_EMBEDDINGS, RAIZ, BuscaNumPy,  # noqa: E402
                             memoria_processo_mb)

SAIDA = RAIZ / "resultados" / "numpy"


def ler_consultas(caminho):
    with open(caminho, encoding="utf-8-sig") as f:
        consultas = [(l["consulta_id"].strip(), l["consulta"].strip()) for l in csv.DictReader(f)]
    if not consultas:
        raise SystemExit(f"nenhuma consulta em {caminho}")
    return consultas


def resumo_tempos(tempos):
    t = np.asarray(tempos)
    return {"n": int(t.size), "media_ms": round(float(t.mean()), 4),
            "p50_ms": round(float(np.percentile(t, 50)), 4),
            "p95_ms": round(float(np.percentile(t, 95)), 4),
            "min_ms": round(float(t.min()), 4), "max_ms": round(float(t.max()), 4)}


def medir(busca, vetores, k, repeticoes, aquecimento, semente):
    """Latencia de busca.buscar_ids para cada consulta, em ordem aleatoria."""
    for v in vetores.values():
        for _ in range(aquecimento):
            busca.buscar_ids(v, k=k)
    ordem = [(cid, r) for cid in vetores for r in range(1, repeticoes + 1)]
    random.Random(semente).shuffle(ordem)
    linhas = []
    for cid, r in ordem:
        inicio = time.perf_counter()
        busca.buscar_ids(vetores[cid], k=k)
        linhas.append((cid, r, (time.perf_counter() - inicio) * 1000))
    return linhas


def extra_da_busca(busca, mem_antes=None, mem_depois=None):
    return {"tempo_preparo_ms": round(busca.tempo_preparo_ms, 3),
            "trechos": int(busca.matriz.shape[0]),
            "memoria_matriz_mb": round(busca.memoria_matriz_mb, 2),
            "memoria_processo_delta_mb": (round(mem_depois - mem_antes, 2)
                                          if mem_antes is not None and mem_depois is not None
                                          else None)}


def busca_reduzida(completa, vetores_trechos, n_docs, semente):
    """BuscaNumPy com so n_docs peticoes sorteadas (mesmos vetores)."""
    docs = sorted(completa.ids_documentos)
    escolhidos = set(random.Random(semente).sample(docs, n_docs))
    pos = [i for i, d in enumerate(completa.doc_dos_trechos) if d in escolhidos]
    mem_antes = memoria_processo_mb()
    busca = BuscaNumPy(carregar_modelo=False, vetores=vetores_trechos[pos],
                       trechos=[completa.trechos[i] for i in pos])
    mem_depois = memoria_processo_mb()
    return busca, extra_da_busca(busca, mem_antes, mem_depois)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("consultas", help="CSV com consulta_id,consulta")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--repeticoes", type=int, default=30)
    p.add_argument("--aquecimento", type=int, default=3)
    p.add_argument("--escalas", default="",
                   help="numeros de peticoes para o teste de escala, ex.: 25,50,99")
    p.add_argument("--semente", type=int, default=42)
    p.add_argument("--saida", default=str(SAIDA))
    args = p.parse_args()

    saida = Path(args.saida)
    saida.mkdir(parents=True, exist_ok=True)
    consultas = ler_consultas(args.consultas)

    mem_inicio = memoria_processo_mb()
    busca = BuscaNumPy()
    mem_carregado = memoria_processo_mb()
    print(f"matriz: {busca.matriz.shape[0]} trechos x {busca.matriz.shape[1]} "
          f"de {busca.n_documentos} peticoes")

    # 1) texto -> vetor (uma vez por consulta) e resultados do top k
    vetores, linhas_cod, linhas_res = {}, [], []
    for cid, texto in consultas:
        inicio = time.perf_counter()
        vetores[cid] = busca.codificar(texto)
        linhas_cod.append((cid, round((time.perf_counter() - inicio) * 1000, 3)))
        for rank, (doc, nota, pos) in enumerate(busca.buscar_trechos(vetores[cid], args.k), 1):
            t = busca.trechos[pos]
            linhas_res.append((cid, texto, rank, doc, round(nota, 6), t["chunk_id"],
                               t["page_start"], t["page_end"]))

    with open(saida / "resultados.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["consulta_id", "consulta", "rank", "document_id", "score",
                    "chunk_id", "page_start", "page_end"])
        w.writerows(linhas_res)
    with open(saida / "codificacao.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["consulta_id", "tempo_ms"])
        w.writerows(linhas_cod)

    # 2) latencia de recuperacao na base completa e nas escalas pedidas
    escalas = {busca.n_documentos: (busca, extra_da_busca(busca))}
    if args.escalas:
        vetores_trechos = np.load(DIR_EMBEDDINGS / "embeddings.npy")
        for n in sorted({int(x) for x in args.escalas.split(",") if x.strip()}):
            if n < busca.n_documentos:
                escalas[n] = busca_reduzida(busca, vetores_trechos, n, args.semente)

    linhas_lat, resumo_escalas = [], {}
    for n in sorted(escalas):
        b, extra = escalas[n]
        medidas = medir(b, vetores, args.k, args.repeticoes, args.aquecimento, args.semente)
        linhas_lat += [(n, cid, r, round(ms, 5)) for cid, r, ms in medidas]
        resumo_escalas[str(n)] = {"documentos": n, **extra,
                                  "recuperacao": resumo_tempos([m[2] for m in medidas])}
        r = resumo_escalas[str(n)]["recuperacao"]
        print(f"{n:>4} peticoes ({extra['trechos']} trechos): "
              f"media {r['media_ms']:.3f} ms | P50 {r['p50_ms']:.3f} | P95 {r['p95_ms']:.3f}")

    with open(saida / "latencias.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["escala_documentos", "consulta_id", "repeticao", "tempo_ms"])
        w.writerows(linhas_lat)

    resumo = {
        "sistema": "NumPy - embeddings em memoria, cosseno direto (matriz @ vetor), "
                   "agregacao max por peticao",
        "consultas": len(consultas), "k": args.k, "repeticoes": args.repeticoes,
        "aquecimento": args.aquecimento, "semente": args.semente,
        "modelo_embeddings": busca.info_embeddings["modelo"],
        "numpy_versao": np.__version__,
        "codificacao_consulta": resumo_tempos([c[1] for c in linhas_cod]),
        "memoria_processo_carregado_mb": (round(mem_carregado - mem_inicio, 1)
                                          if mem_inicio is not None else None),
        "escalas": resumo_escalas,
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
    }
    (saida / "resumo.json").write_text(json.dumps(resumo, indent=2, ensure_ascii=False),
                                       encoding="utf-8")
    print(f"\nresultados em {saida}")


if __name__ == "__main__":
    main()
