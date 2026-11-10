# -*- coding: utf-8 -*-
"""
Transforma cada trecho em um vetor (embedding) e grava o indice vetorial.

Entrada : data/03_chunks/chunks.jsonl (saida do dividir_trechos.py)
Saida   : data/04_embeddings/embeddings.npy -> matriz float32 (n_trechos x dimensao),
                                         linha i = trecho da linha i do chunks.jsonl
          data/04_embeddings/chunks.jsonl -> copia dos trechos, na mesma ordem
          data/04_embeddings/info.json    -> modelo, dimensao, quantos trechos, data

Modelo: intfloat/multilingual-e5-base (linha de base da etapa 5: pronto, sem
treino, entende portugues, roda na CPU). O e5 foi treinado com prefixos:
"passage: " no texto indexado e "query: " na consulta -- sem eles a qualidade
cai, por isso o prefixo e colocado aqui e no buscar.py.

Os vetores saem normalizados (tamanho 1), entao a similaridade de cosseno vira
um produto escalar simples. A busca (src/busca_numpy.py) carrega este .npy na
memoria e compara a consulta com todos os trechos. A
copia dos trechos vai junto pro indice ficar sempre alinhado com os vetores,
mesmo se o dividir_trechos.py rodar de novo depois.

Na primeira rodada o modelo (~1,1 GB) e baixado do Hugging Face e fica em cache.

Checkpoint: os vetores sao gravados em blocos (--bloco trechos por vez) em
data/04_embeddings/_parcial/. Se o processo cair (falta de memoria, por exemplo),
rodar de novo retoma do ultimo bloco salvo. O _parcial/ e apagado no fim. Ele so
e reaproveitado se o modelo e os trechos forem os mesmos (confere por hash).

Uso:
    python scripts/gerar_embeddings.py
    python scripts/gerar_embeddings.py --modelo intfloat/multilingual-e5-small   (mais rapido)
"""

import argparse
import hashlib
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

RAIZ = Path(__file__).resolve().parent.parent
TRECHOS = RAIZ / "data" / "03_chunks" / "chunks.jsonl"
INDICE = RAIZ / "data" / "04_embeddings"
MODELO = "intfloat/multilingual-e5-base"
LOTE = 8      # lote pequeno: menos memoria de pico, quase a mesma velocidade na CPU
BLOCO = 256   # trechos por checkpoint


def carrega_trechos(caminho):
    with open(caminho, encoding="utf-8") as f:
        return [json.loads(linha) for linha in f if linha.strip()]


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--trechos", default=str(TRECHOS))
    p.add_argument("--indice", default=str(INDICE), help="pasta de saida")
    p.add_argument("--modelo", default=MODELO)
    p.add_argument("--lote", type=int, default=LOTE)
    p.add_argument("--bloco", type=int, default=BLOCO, help="trechos por checkpoint")
    args = p.parse_args()

    trechos = carrega_trechos(args.trechos)
    if not trechos:
        raise SystemExit(f"nenhum trecho em {args.trechos} (rode o dividir_trechos.py antes)")
    print(f"{len(trechos)} trechos; carregando {args.modelo} ...")

    pasta = Path(args.indice)
    parcial = pasta / "_parcial"
    assinatura = hashlib.sha256(
        (args.modelo + "|" + "|".join(t["chunk_id"] + t["text"] for t in trechos))
        .encode("utf-8")).hexdigest()
    arquivo_assinatura = parcial / "assinatura.txt"
    if arquivo_assinatura.exists() and arquivo_assinatura.read_text() != assinatura:
        shutil.rmtree(parcial)  # checkpoint de outro modelo/outros trechos
    parcial.mkdir(parents=True, exist_ok=True)
    arquivo_assinatura.write_text(assinatura)

    modelo = SentenceTransformer(args.modelo, device="cpu")
    modelo.max_seq_length = 512
    blocos = range(0, len(trechos), args.bloco)
    feitos = sum((parcial / f"{b:06d}.npy").exists() for b in blocos)
    if feitos:
        print(f"retomando: {feitos} de {len(blocos)} blocos ja salvos")
    inicio = time.time()
    for n, b in enumerate(blocos, 1):
        arquivo = parcial / f"{b:06d}.npy"
        if arquivo.exists():
            continue
        lote = trechos[b:b + args.bloco]
        v = modelo.encode(["passage: " + t["text"] for t in lote], batch_size=args.lote,
                          normalize_embeddings=True, convert_to_numpy=True).astype(np.float32)
        np.save(parcial / f"{b:06d}.tmp.npy", v)
        (parcial / f"{b:06d}.tmp.npy").replace(arquivo)   # bloco so conta quando completo
        decorrido = time.time() - inicio
        print(f"bloco {n}/{len(blocos)} ({b + len(lote)} trechos) - {decorrido / 60:.1f} min", flush=True)
    segundos = time.time() - inicio
    vetores = np.concatenate([np.load(parcial / f"{b:06d}.npy") for b in blocos])

    pasta.mkdir(parents=True, exist_ok=True)
    np.save(pasta / "embeddings.npy", vetores)
    shutil.copyfile(args.trechos, pasta / "chunks.jsonl")
    info = {
        "modelo": args.modelo,
        "prefixo_trecho": "passage: ",
        "prefixo_consulta": "query: ",
        "dimensao": int(vetores.shape[1]),
        "trechos": len(trechos),
        "documentos": len({t["document_id"] for t in trechos}),
        "segundos": round(segundos, 1),
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
    }
    (pasta / "info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(parcial)
    print(f"{vetores.shape[0]} vetores de {vetores.shape[1]} dimensoes em {segundos:.0f} s -> {pasta}")


if __name__ == "__main__":
    main()
