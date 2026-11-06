# -*- coding: utf-8 -*-
"""
Testes do scripts/dividir_trechos.py: corte das janelas e paginas de cada trecho.

Rodar:
    pytest tests/
"""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))

import dividir_trechos as dt  # noqa: E402


# --------------------------------------------------------------------- janelas
def test_documento_vazio_nao_gera_trecho():
    assert dt.janelas(0) == []


def test_documento_curto_vira_um_trecho_so():
    assert dt.janelas(120, tamanho=250, passo=200) == [(0, 120)]


def test_janelas_cobrem_tudo_com_sobreposicao():
    js = dt.janelas(500, tamanho=250, passo=200)
    assert js == [(0, 250), (200, 450), (400, 500)]
    assert js[-1][1] == 500


def test_sobra_pequena_e_grudada_no_trecho_anterior():
    # 260 palavras: a sobra (10 novas) nao vira um trecho de 60 palavras repetidas
    assert dt.janelas(260, tamanho=250, passo=200, min_palavras=30) == [(0, 260)]


def test_nenhuma_palavra_fica_de_fora():
    for n in range(1, 2000, 37):
        js = dt.janelas(n, tamanho=250, passo=200)
        assert js[0][0] == 0 and js[-1][1] == n
        for (_, fim), (ini, _) in zip(js, js[1:]):
            assert ini < fim  # sempre encosta ou sobrepoe, nunca deixa buraco


# ------------------------------------------------------------ trechos/paginas
def _doc(*textos):
    return {"id": "ADI_1", "pages": [{"page": i, "text": t} for i, t in enumerate(textos, 1)]}


def test_pagina_quase_vazia_e_ignorada():
    fila = dt.palavras_com_pagina(_doc("carimbo", "texto " * 30)["pages"])
    assert {pag for _, pag in fila} == {2}


def test_trecho_sabe_pagina_inicial_e_final():
    doc = _doc("a " * 100, "b " * 100, "c " * 100)  # 300 palavras em 3 paginas
    ts = list(dt.trechos_do_documento(doc, {"processo": "ADI 1"}, tamanho=150, passo=150))
    assert [(t["page_start"], t["page_end"]) for t in ts] == [(1, 2), (2, 3)]
    assert ts[0]["chunk_id"] == "ADI_1_0001"
    assert ts[0]["processo"] == "ADI 1"
    assert all(t["palavras"] == 150 for t in ts)
