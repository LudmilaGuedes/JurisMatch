# -*- coding: utf-8 -*-
"""
Testes das funcoes "puras" do scripts/extrair_texto.py: as que decidem
coisas sem precisar do Tesseract nem de um PDF de verdade em disco.

O que envolve Tesseract (o OCR em si) nao esta aqui: e testado na mao,
rodando o script na amostra (data/amostra/) e conferindo o resultado.

Rodar:
    .venv/bin/pytest tests/
"""

import sys
from pathlib import Path

import pymupdf
import pytest

# scripts/ nao e um pacote Python, entao coloco a pasta no caminho de
# import na mao. Assim "import extrair_texto" funciona de qualquer lugar.
RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "scripts"))

import extrair_texto as ext  # noqa: E402  (precisa vir depois do sys.path)


# ---------------------------------------------------------------- precisa_ocr
# Os numeros abaixo vieram da amostra e do levantamento do corpus (ver README,
# etapa 3):
#   escaneado puro:      0 chars,     cobertura 0.90-1.00
#   OCR antigo ruim:     ~2600 chars, cobertura 1.00, texto invisivel (0.0)
#   nativo do Word:      ~2000 chars, cobertura 0.00-0.12, texto visivel (1.0)
#   Word com timbrado:   ~2000 chars, cobertura ~1.00, texto visivel (1.0)


def test_pagina_escaneada_sem_texto_precisa_ocr():
    assert ext.precisa_ocr(chars_pdf=0, cobertura_imagem=0.98, texto_visivel=0.0) is True


def test_pagina_com_ocr_antigo_por_cima_da_imagem_precisa_ocr():
    # tem texto, mas a pagina inteira e uma imagem e o texto e a camada
    # invisivel do Acrobat, que sai ruim ("Questlo de ordem")
    assert ext.precisa_ocr(chars_pdf=2658, cobertura_imagem=1.0, texto_visivel=0.0) is True


def test_pagina_nativa_nao_precisa_ocr():
    assert ext.precisa_ocr(chars_pdf=1786, cobertura_imagem=0.0, texto_visivel=1.0) is False


def test_pagina_nativa_com_logo_pequeno_nao_precisa_ocr():
    # ADPF 1300: logo do escritorio cobre 12% da pagina, o texto e nativo
    assert ext.precisa_ocr(chars_pdf=1621, cobertura_imagem=0.12, texto_visivel=1.0) is False


def test_pagina_nativa_com_timbrado_de_fundo_nao_precisa_ocr():
    # ADI 7845: o timbrado e uma imagem do tamanho da pagina, mas o texto
    # do Word esta visivel por cima dela. O texto nativo e o bom.
    assert ext.precisa_ocr(chars_pdf=1545, cobertura_imagem=1.0, texto_visivel=1.0) is False


def test_pagina_quase_vazia_precisa_ocr():
    # menos de 50 chars e nenhuma imagem grande: pode ser uma pagina em
    # branco ou algo que o PyMuPDF nao entendeu. OCR nao custa nada.
    assert ext.precisa_ocr(chars_pdf=30, cobertura_imagem=0.0, texto_visivel=1.0) is True


def test_limiar_de_cobertura_e_meia_pagina():
    assert ext.precisa_ocr(chars_pdf=2000, cobertura_imagem=0.49, texto_visivel=0.0) is False
    assert ext.precisa_ocr(chars_pdf=2000, cobertura_imagem=0.50, texto_visivel=0.0) is True


def test_limiar_de_texto_visivel_e_metade():
    assert ext.precisa_ocr(chars_pdf=2000, cobertura_imagem=1.0, texto_visivel=0.49) is True
    assert ext.precisa_ocr(chars_pdf=2000, cobertura_imagem=1.0, texto_visivel=0.50) is False


# ---------------------------------------------------------------- giro
# Numeros do teste com as 151 paginas em que o OSD sugeriu giro (README).


def test_ocr_com_confianca_boa_nem_pergunta_o_giro():
    assert ext.vale_perguntar_giro(85) is False


def test_ocr_com_confianca_baixa_pergunta_o_giro():
    assert ext.vale_perguntar_giro(41) is True


def test_pagina_de_lado_de_verdade_fica_girada():
    # ADI 2039 pagina 83: 41 em pe, 88 girada
    assert ext.fica_com_girada(confianca_original=41, confianca_girada=88) is True


def test_osd_errado_nao_estraga_pagina_em_pe():
    # ADPF 165 pagina 636: o OSD mandou girar 180 com confianca alta, mas
    # a pagina estava em pe (88) e girada virou lixo (41)
    assert ext.fica_com_girada(confianca_original=88, confianca_girada=41) is False


def test_ganho_pequeno_nao_justifica_girar():
    # ADI 2945 pagina 132 (tabela): 79 x 85, empate tecnico, fica como esta
    assert ext.fica_com_girada(confianca_original=79, confianca_girada=85) is False


# ---------------------------------------------------------------- cobertura_imagem
# Aqui uso um PDF criado na hora pelo PyMuPDF, entao o teste roda sem
# depender de nenhum arquivo em data/.


def _pagina_com_imagem(fracao_da_pagina):
    """Cria um PDF de uma pagina com uma imagem cobrindo a fracao pedida."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4 em pontos
    if fracao_da_pagina > 0:
        # imagem cinza de 10x10 px; o tamanho em que ela aparece na pagina
        # e o que importa, nao o tamanho do arquivo
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 10, 10))
        pix.clear_with(128)
        rect = pymupdf.Rect(0, 0, 595, 842 * fracao_da_pagina)
        page.insert_image(rect, pixmap=pix)
    # reabre pelos bytes: e assim que o script vai ver o PDF em disco
    return pymupdf.open("pdf", doc.tobytes())[0]


def test_pagina_sem_imagem_tem_cobertura_zero():
    assert ext.cobertura_imagem(_pagina_com_imagem(0)) == 0.0


def test_pagina_toda_imagem_tem_cobertura_um():
    assert ext.cobertura_imagem(_pagina_com_imagem(1.0)) == pytest.approx(1.0, abs=0.01)


def test_pagina_meia_imagem_tem_cobertura_meio():
    assert ext.cobertura_imagem(_pagina_com_imagem(0.5)) == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------- texto_visivel
# PDFs de mentira com uma imagem de pagina inteira e um texto, mudando so a
# ordem (texto antes ou depois da imagem) e o modo (visivel ou invisivel).


def _pagina_imagem_e_texto(texto_primeiro=False, invisivel=False):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 10, 10))
    pix.clear_with(255)
    modo = 3 if invisivel else 0  # 3 = texto invisivel, como o OCR do Acrobat
    if texto_primeiro:
        page.insert_text((72, 100), "Excelentissimo Senhor Ministro", render_mode=modo)
        page.insert_image(page.rect, pixmap=pix)
    else:
        page.insert_image(page.rect, pixmap=pix)
        page.insert_text((72, 100), "Excelentissimo Senhor Ministro", render_mode=modo)
    return pymupdf.open("pdf", doc.tobytes())[0]


def test_timbrado_com_texto_por_cima_e_visivel():
    assert ext.texto_visivel(_pagina_imagem_e_texto()) == pytest.approx(1.0)


def test_texto_invisivel_por_cima_da_imagem_nao_e_visivel():
    assert ext.texto_visivel(_pagina_imagem_e_texto(invisivel=True)) == 0.0


def test_texto_embaixo_da_imagem_nao_e_visivel():
    assert ext.texto_visivel(_pagina_imagem_e_texto(texto_primeiro=True)) == 0.0


def test_pagina_sem_texto_tem_visivel_zero():
    assert ext.texto_visivel(_pagina_com_imagem(1.0)) == 0.0


# ---------------------------------------------------------------- nomes


def test_nome_saida_espelha_o_pdf_com_a_classe_como_pasta():
    pdf = Path("dados/iniciais/ADI/ADI_2945_inicial_atualizada.pdf")
    saida = Path("dados/textos")
    assert ext.nome_saida(pdf, saida) == Path("dados/textos/ADI/ADI_2945_inicial_atualizada.jsonl")


def test_nome_saida_tira_a_classe_do_nome_e_nao_da_pasta():
    # a amostra e uma pasta plana, sem ADI/ ADPF/ etc. A classe vem do nome.
    pdf = Path("dados/amostra/amostra_iniciais/ADPF_165_inicial.pdf")
    saida = Path("dados/amostra/textos")
    assert ext.nome_saida(pdf, saida) == Path("dados/amostra/textos/ADPF/ADPF_165_inicial.jsonl")


def test_processo_do_nome():
    assert ext.processo_do_nome("ADI_2945_inicial_atualizada.pdf") == "ADI 2945"
    assert ext.processo_do_nome("ADPF_165_inicial.pdf") == "ADPF 165"
