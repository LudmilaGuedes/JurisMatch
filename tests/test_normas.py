# -*- coding: utf-8 -*-
"""Testes do tipo de norma impugnada (src/normas.py), usado no filtro do front."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.normas import MEDIDA_PROVISORIA, OUTROS, normas_impugnadas, tipos_norma  # noqa: E402


def test_uma_norma():
    leg = "1 - Lei Federal nº 8429 de 1992, Art. 12 - Art. 12, II e III da Lei nº 8.429"
    assert tipos_norma(leg) == {"Lei federal"}


def test_varias_normas_na_mesma_linha_e_com_quebra():
    leg = ("1 - Decreto nº 11190 de 2022 - Altera o Decreto nº 10.961. "
           "2 - Medida Provisória nº 1136 de 2022 - Em face da Medida Provisória_x000D_\n"
           "3 - Lei Complementar Estadual nº 10 de 2001")
    assert len(normas_impugnadas(leg)) == 3
    assert tipos_norma(leg) == {"Decreto ou decreto-lei", MEDIDA_PROVISORIA, "Lei complementar"}


def test_lei_de_conversao_conta_como_medida_provisoria():
    leg = ("1 - Lei Federal nº 13452 de 2017 - Em face da Lei Federal nº 13.452 "
           "(Conversão da Medida Provisória nº 758, de 2016)")
    assert tipos_norma(leg) == {"Lei federal", MEDIDA_PROVISORIA}
    leg2 = "1 - Lei Federal nº 14173 de 2021 - a partir da conversão em lei da Medida Provisória nº 1.018"
    assert MEDIDA_PROVISORIA in tipos_norma(leg2)


def test_mp_de_ministerio_publico_nao_e_medida_provisoria():
    leg = "1 - Lei Federal nº 9615 de 1998 - e a legitimidade do MP para celebrar acordos"
    assert tipos_norma(leg) == {"Lei federal"}


def test_numero_dentro_do_texto_nao_separa_norma():
    leg = "1 - Lei Estadual nº 123 de 2020 - arts. 2 - 5 da lei"
    assert normas_impugnadas(leg) == ["Lei Estadual nº 123 de 2020 - arts. 2 - 5 da lei"]


def test_desconhecido_e_vazio():
    assert tipos_norma("1 - Regimento Interno - Artigo 44") == {OUTROS}
    assert tipos_norma("") == set()
