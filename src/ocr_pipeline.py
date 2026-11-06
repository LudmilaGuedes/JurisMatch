# -*- coding: utf-8 -*-
"""
Pipeline de extracao de texto das peticoes iniciais do STF (Fase 2).

Fluxo por documento (o ID e o nome do PDF, ex.: ADI_6002):
    data/01_raw/pdf/<ID>.pdf
        -> decisao pagina a pagina (texto digital x OCR)
        -> data/02_processed/ocr/<ID>.json     texto por pagina, como saiu
        -> limpeza
        -> data/02_processed/clean/<ID>.json   texto limpo, por pagina
        -> metadata/documentos.csv             colunas de status/qualidade
        -> logs/processing.csv                 uma linha por documento e etapa

Os PDFs chegam em data/01_raw/pdf/ pelo scripts/00_registrar_documentos.py.

Uso (linha de comando):
    python src/ocr_pipeline.py
    python src/ocr_pipeline.py --workers 3
    python src/ocr_pipeline.py --refazer        # refaz tudo, inclusive o OCR
    python src/ocr_pipeline.py --relimpar       # refaz so a limpeza

Uso (notebook):
    from ocr_pipeline import processar_pasta
    df = processar_pasta()
"""

from __future__ import annotations

import argparse
import datetime as _dt
import gc
import hashlib
import io
import json
import os
import re
import shutil
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import fitz  # PyMuPDF
import pandas as pd
from PIL import Image

VERSAO_PIPELINE = "2.1"

# ---------------------------------------------------------------------------
# Pastas (relativas a raiz do projeto, e nao a pasta de onde se roda)
# ---------------------------------------------------------------------------

RAIZ = Path(__file__).resolve().parent.parent
DIR_PDF = RAIZ / "data" / "01_raw" / "pdf"
DIR_PROCESSADO = RAIZ / "data" / "02_processed"
CAMINHO_DOCUMENTOS = RAIZ / "metadata" / "documentos.csv"
CAMINHO_LOG = RAIZ / "logs" / "processing.csv"

# ---------------------------------------------------------------------------
# Parametros ajustaveis
# ---------------------------------------------------------------------------

# Minimo de caracteres uteis para considerar que a PAGINA tem camada textual.
# Abaixo disso a pagina e tratada como imagem e vai para o OCR.
MIN_CHARS_PAGINA = 100

# Qualidade da camada textual. Ter texto nao basta: PDF com fonte mal embutida
# ou com OCR antigo de baixa qualidade tem camada textual, mas ela e lixo.
# Acima destas proporcoes a pagina e reprocessada por OCR (metodo ocr_fallback).
# Calibrado na base: com estas regras nenhuma das ~2.400 paginas digitais boas
# e reprovada; percentuais, numeracao de secao (II.1.2) e '28-A' nao contam.
MAX_PROP_TOKENS_SUSPEITOS = 0.05
MAX_PROP_CHARS_INVALIDOS = 0.02
# Abaixo deste numero de palavras a proporcao nao e confiavel e a pagina passa.
MIN_TOKENS_AVALIACAO = 20

# OCR de figuras embutidas em paginas digitais (print de tabela, de lei, de
# decisao). Sem isso o texto dessas imagens some da pagina digital.
OCR_FIGURAS = True
MIN_LARGURA_FIGURA = 400
MIN_ALTURA_FIGURA = 300
# Imagem que cobre mais que isso da pagina e o proprio escaneamento: a camada
# textual ja e o texto dela, e reler duplicaria a pagina.
MAX_COBERTURA_FIGURA = 0.6
# Figura com menos palavras que isso e logotipo/brasao, nao conteudo.
MIN_PALAVRAS_FIGURA = 10

# DPI de renderizacao da pagina antes do OCR.
DPI_PADRAO = 300
# DPI usado na segunda tentativa, quando a confianca da primeira ficou baixa.
DPI_REFORCO = 400
# Confianca media (0-100) abaixo da qual a pagina e reprocessada em DPI maior.
CONF_MINIMA = 70.0
# A cada quantas paginas de OCR o progresso do documento e salvo em disco.
SALVAR_A_CADA = 25

IDIOMA_OCR = "por"
CONFIG_OCR = "--oem 1 --psm 3"

# Uma linha de topo/rodape que se repete em pelo menos esta fracao das paginas
# do trecho em que aparece e tratada como cabecalho/rodape (carimbo) e removida
# na limpeza.
FRACAO_REPETICAO = 0.6
# ...desde que apareca em pelo menos este numero de paginas.
MIN_PAGINAS_BOILERPLATE = 5
# Quantas linhas do inicio e do fim de cada pagina entram nessa checagem.
# 5 porque timbre de escritorio costuma ocupar 3 ou 4 linhas.
LINHAS_BORDA = 5

# ---------------------------------------------------------------------------
# Tesseract
# ---------------------------------------------------------------------------

CAMINHOS_TESSERACT = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
]


def configurar_tesseract(caminho=None):
    """Localiza o Tesseract e devolve (pytesseract, mensagem) ou (None, motivo)."""
    try:
        import pytesseract
    except ImportError:
        return None, "biblioteca pytesseract nao instalada (pip install pytesseract)"

    candidatos = [caminho] if caminho else []
    candidatos += CAMINHOS_TESSERACT

    achado = None
    for c in candidatos:
        if c and os.path.isfile(c):
            achado = c
            break
    if achado is None:
        achado = shutil.which("tesseract")

    if achado is None:
        return None, "executavel do Tesseract nao encontrado (instale o Tesseract-OCR)"

    pytesseract.pytesseract.tesseract_cmd = achado
    try:
        idiomas = pytesseract.get_languages(config="")
    except Exception as e:
        return None, "Tesseract encontrado em %s, mas falhou ao rodar: %s" % (achado, e)

    if IDIOMA_OCR not in idiomas:
        return None, (
            "Tesseract encontrado em %s, mas sem o pacote de idioma '%s' "
            "(idiomas disponiveis: %s)" % (achado, IDIOMA_OCR, ", ".join(idiomas))
        )
    return pytesseract, "Tesseract OK em %s (idiomas: %s)" % (achado, ", ".join(idiomas))


# ---------------------------------------------------------------------------
# Etapa 1 - Extracao
# ---------------------------------------------------------------------------

def _texto_util(texto):
    """Texto sem espacos/quebras, usado so para medir se a pagina tem conteudo."""
    return re.sub(r"\s+", "", texto or "")


# Pontuacao comum em texto juridico, que nao indica lixo dentro de um token:
# '27,5%', 'R$', '13.465/2017', '§1º', '(doc.', aspas e travessoes.
_PONTUACAO_NORMAL = set("-/ºª°§._,%$()[]'\"“”‘’–—:;!?")
_VOGAIS = set("aeiouáéíóúâêôãõàü")


def token_suspeito(token):
    """Token com cara de erro de extracao ('c0nst1tu1cao', 'Ã§Ã£', 'xkcdfgh')."""
    t = token.strip(".,;:!?()[]{}<>\"'“”")
    if len(t) < 4 or "http" in t.lower() or "www" in t.lower():
        return False

    # Letras e digitos alternando mais de uma vez. Uma transicao e normal
    # ('ADI6002', 'art.5', 'II.1.2', 'EC95'); duas ou mais e lixo ('c0nst1').
    classes = ["a" if c.isalpha() and c not in "ºª" else "d"
               for c in t if c.isalnum() and c not in "ºª"]
    transicoes = sum(1 for x, y in zip(classes, classes[1:]) if x != y)
    if transicoes >= 2:
        return True

    if sum(1 for c in t if not c.isalnum() and c not in _PONTUACAO_NORMAL) >= 2:
        return True
    if re.search(r"([^\W\d_])\1{3,}", t, re.IGNORECASE):
        return True
    letras = [c.lower() for c in t if c.isalpha()]
    if len(letras) >= 6 and not _VOGAIS.intersection(letras):
        return True
    return False


def _char_invalido(c):
    """Caractere de substituicao, de uso privado ou de controle: fonte quebrada."""
    if c in "\n\r\t":
        return False
    return c == "�" or unicodedata.category(c) in ("Co", "Cc")


def avaliar_camada_textual(texto):
    """Devolve None se a camada textual da pagina e boa, ou o motivo da reprovacao.

    Pega dois defeitos comuns: fonte embutida sem tabela de caracteres (o texto
    sai como U+FFFD ou simbolos de uso privado) e OCR antigo de baixa
    qualidade gravado no PDF. Nos dois casos a pagina tem 'texto', e a checagem
    so de tamanho a aceitaria.
    """
    if not texto:
        return None
    invalidos = sum(1 for c in texto if _char_invalido(c))
    if invalidos / len(texto) > MAX_PROP_CHARS_INVALIDOS:
        return "caracteres invalidos (%d)" % invalidos

    tokens = texto.split()
    if len(tokens) < MIN_TOKENS_AVALIACAO:
        return None
    suspeitos = sum(1 for t in tokens if token_suspeito(t))
    if suspeitos / len(tokens) > MAX_PROP_TOKENS_SUSPEITOS:
        return "tokens suspeitos (%d de %d)" % (suspeitos, len(tokens))
    return None


def ocr_figuras(pagina, pytesseract, vistas):
    """OCR das figuras grandes embutidas numa pagina digital.

    'vistas' guarda os xrefs ja tratados no documento: a mesma imagem repetida
    em toda pagina (timbre, brasao) e lida uma vez so. Devolve o texto das
    figuras, ou '' se nenhuma tiver texto.
    """
    area_pagina = abs(pagina.rect.get_area()) or 1.0
    partes = []
    for info in pagina.get_image_info(xrefs=True):
        xref = info.get("xref") or 0
        if not xref or xref in vistas:
            continue
        vistas.add(xref)
        if info["width"] < MIN_LARGURA_FIGURA or info["height"] < MIN_ALTURA_FIGURA:
            continue
        if abs(fitz.Rect(info["bbox"]).get_area()) / area_pagina > MAX_COBERTURA_FIGURA:
            continue
        try:
            bruto = pagina.parent.extract_image(xref)
            imagem = Image.open(io.BytesIO(bruto["image"])).convert("L")
            texto = pytesseract.image_to_string(imagem, lang=IDIOMA_OCR, config=CONFIG_OCR)
        except Exception:
            # Formato de imagem que o PIL nao abre (JBIG2, JPX...): segue sem ela.
            continue
        if len(texto.split()) >= MIN_PALAVRAS_FIGURA:
            # Termina em ':' para o reagrupamento nao colar o rotulo no texto.
            partes.append("[figura embutida, texto via OCR]:\n" + texto.strip())
    return "\n\n".join(partes)


def _renderizar(pagina, dpi=DPI_PADRAO, cinza=False, rotacao=0):
    """Pagina do PDF como imagem PIL, opcionalmente girada (graus, anti-horario)."""
    matriz = fitz.Matrix(dpi / 72, dpi / 72)
    espaco = fitz.csGRAY if cinza else fitz.csRGB
    pix = pagina.get_pixmap(matrix=matriz, colorspace=espaco)
    imagem = Image.open(io.BytesIO(pix.tobytes("png")))
    del pix
    if rotacao:
        imagem = imagem.rotate(rotacao, expand=True, fillcolor="white")
    return imagem


def ocr_pagina(pagina, pytesseract, dpi=DPI_PADRAO, cinza=False, rotacao=0):
    """Renderiza a pagina como imagem e roda o OCR. Devolve (texto, confianca)."""
    imagem = _renderizar(pagina, dpi=dpi, cinza=cinza, rotacao=rotacao)

    dados = pytesseract.image_to_data(
        imagem,
        lang=IDIOMA_OCR,
        config=CONFIG_OCR,
        output_type=pytesseract.Output.DICT,
    )

    # Reconstroi o texto respeitando blocos/paragrafos/linhas e coleta a confianca
    # palavra a palavra (por isso image_to_data e nao image_to_string).
    linhas = {}
    confiancas = []
    for i, palavra in enumerate(dados["text"]):
        if not palavra.strip():
            continue
        chave = (dados["block_num"][i], dados["par_num"][i], dados["line_num"][i])
        linhas.setdefault(chave, []).append(palavra)
        try:
            conf = float(dados["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf >= 0:
            confiancas.append(conf)

    texto = "\n".join(" ".join(p) for _, p in sorted(linhas.items()))
    confianca = round(sum(confiancas) / len(confiancas), 1) if confiancas else 0.0

    del imagem, dados
    return texto, confianca


def detectar_rotacao(pagina, pytesseract):
    """Pergunta ao Tesseract (modulo OSD) se a pagina esta girada.

    Devolve os graus que o OSD sugere (0, 90, 180 ou 270), ou 0 se ele nao
    conseguir decidir - o que acontece em pagina com pouco texto.
    """
    try:
        imagem = _renderizar(pagina, dpi=DPI_PADRAO, cinza=True)
        osd = pytesseract.image_to_osd(imagem, output_type=pytesseract.Output.DICT)
        return int(osd.get("rotate", 0)) % 360
    except Exception:
        return 0


def ocr_com_reforco(pagina, pytesseract, dpi=DPI_PADRAO, verbose=False):
    """OCR de uma pagina com as tentativas extras quando a leitura sai ruim.

    1. Leitura normal, no DPI padrao.
    2. Confianca abaixo de CONF_MINIMA: refaz em DPI_REFORCO e escala de cinza.
    3. Ainda baixa: verifica se a pagina esta girada (de cabeca para baixo ou
       de lado, comum em anexos escaneados) e, se estiver, le de novo na
       orientacao certa. Sem isso o texto de uma pagina invertida sai como
       'VIYNIDUVYW OHNIHAOS' - perdido, apesar de a pagina ter conteudo.

    Fica sempre o resultado de maior confianca. Devolve um dict com texto,
    conf, dpi e rotacao (graus aplicados; 0 se a pagina nao foi girada).
    """
    texto, conf = ocr_pagina(pagina, pytesseract, dpi=dpi)
    melhor = {"texto": texto, "conf": conf, "dpi": dpi, "rotacao": 0}

    if melhor["conf"] < CONF_MINIMA:
        if verbose:
            print("        conf %.1f baixa, refazendo em %d DPI..." % (melhor["conf"], DPI_REFORCO))
        texto, conf = ocr_pagina(pagina, pytesseract, dpi=DPI_REFORCO, cinza=True)
        if conf > melhor["conf"]:
            melhor = {"texto": texto, "conf": conf, "dpi": DPI_REFORCO, "rotacao": 0}

    if melhor["conf"] < CONF_MINIMA:
        melhor = tentar_rotacao(pagina, pytesseract, melhor, dpi=dpi, verbose=verbose)

    return melhor


def tentar_rotacao(pagina, pytesseract, melhor, dpi=DPI_PADRAO, verbose=False):
    """Se o OSD achar a pagina girada, le de novo na orientacao certa.

    Recebe a melhor leitura ate agora e devolve ela mesma ou uma melhor. O OSD
    informa o angulo, mas a convencao de sentido varia (num caso real ele
    sugeriu 90 graus e o certo era 270); testar os dois sentidos custa uma
    leitura a mais, e so nas paginas que estao de fato giradas.
    """
    sugerida = detectar_rotacao(pagina, pytesseract)
    if not sugerida:
        return melhor
    for graus in sorted({sugerida, (360 - sugerida) % 360}):
        if verbose:
            print("        pagina girada? lendo de novo com rotacao de %d graus..." % graus)
        texto, conf = ocr_pagina(pagina, pytesseract, dpi=dpi, cinza=True, rotacao=graus)
        if conf > melhor["conf"]:
            melhor = {"texto": texto, "conf": conf, "dpi": dpi, "rotacao": graus}
    return melhor


def sha256_arquivo(caminho):
    """Hash do PDF, para o parcial so ser reaproveitado se o arquivo nao mudou."""
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def _salvar_parcial(caminho_parcial, hash_pdf, paginas):
    """Grava as paginas ja extraidas; escrita atomica para nao corromper o arquivo."""
    caminho_parcial = Path(caminho_parcial)
    caminho_parcial.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho_parcial.with_suffix(".tmp")
    temporario.write_text(
        json.dumps({"sha256": hash_pdf, "paginas": paginas}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporario, caminho_parcial)


def _ler_parcial(caminho_parcial, hash_pdf):
    """Paginas salvas de uma rodada interrompida, indexadas pelo numero.

    Descarta o parcial se o PDF mudou desde entao (hash diferente) ou se o
    arquivo estiver corrompido.
    """
    try:
        dados = json.loads(Path(caminho_parcial).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(dados, dict) or dados.get("sha256") != hash_pdf:
        return {}
    return {p["numero"]: p for p in dados.get("paginas", [])}


def extrair_documento(caminho_pdf, pytesseract=None, dpi=DPI_PADRAO, verbose=True,
                      motivo_sem_ocr="o Tesseract nao esta disponivel",
                      caminho_parcial=None, paginas_ocr_anteriores=None, hash_pdf=None):
    """Extrai o texto de um PDF decidindo pagina a pagina entre digital e OCR.

    A decisao e por PAGINA (e nao por documento) porque existem peticoes mistas:
    processo nativo digital com algumas paginas anexadas como imagem escaneada.
    Decidir pelo documento inteiro faria essas paginas sairem vazias em silencio.

    Para cada pagina:
      - camada textual com tamanho e qualidade suficientes -> 'digital' (mais o
        OCR das figuras embutidas, se OCR_FIGURAS);
      - camada textual com tamanho, mas lixo -> OCR ('ocr_fallback');
      - sem camada textual -> OCR ('ocr').

    Com caminho_parcial, o progresso e salvo a cada SALVAR_A_CADA paginas de OCR
    e, se existir um parcial de uma rodada interrompida do mesmo PDF, a
    extracao retoma dali. Numa peticao de mais de mil paginas escaneadas uma
    queda perto do fim custaria mais de uma hora de OCR.

    paginas_ocr_anteriores ({numero: pagina}) traz as paginas ja lidas por OCR numa
    extracao anterior do MESMO PDF (quem chama confere o hash). Uma pagina que
    precisa de OCR e esta ali e reaproveitada, sem rodar o Tesseract. As paginas
    digitais sao sempre refeitas: custam milissegundos e assim recebem as
    regras novas (checagem de qualidade, figuras).
    """
    paginas = []
    ja_feitas = {}
    paginas_ocr_anteriores = paginas_ocr_anteriores or {}
    hash_pdf = hash_pdf or sha256_arquivo(caminho_pdf)
    if caminho_parcial and Path(caminho_parcial).exists():
        ja_feitas = _ler_parcial(caminho_parcial, hash_pdf)
        if verbose and ja_feitas:
            print("      retomando: %d pagina(s) ja salvas de uma rodada anterior"
                  % len(ja_feitas))
    ocr_desde_salvo = 0
    figuras_vistas = set()

    with fitz.open(caminho_pdf) as doc:
        total = len(doc)
        for indice in range(total):
            if indice + 1 in ja_feitas:
                paginas.append(ja_feitas[indice + 1])
                continue
            pagina = doc[indice]
            texto_digital = pagina.get_text()

            tem_camada = len(_texto_util(texto_digital)) >= MIN_CHARS_PAGINA
            reprovacao = avaliar_camada_textual(texto_digital) if tem_camada else None

            if tem_camada and not reprovacao:
                texto = texto_digital
                if OCR_FIGURAS and pytesseract is not None:
                    figuras = ocr_figuras(pagina, pytesseract, figuras_vistas)
                    if figuras:
                        texto = texto.rstrip() + "\n\n" + figuras
                paginas.append({
                    "numero": indice + 1,
                    "metodo": "digital",
                    "texto": texto,
                    "conf": None,
                    "dpi": None,
                })
                continue

            # Sem camada textual, ou camada reprovada -> OCR
            anterior = paginas_ocr_anteriores.get(indice + 1)
            if anterior is not None:
                paginas.append(dict(anterior, metodo="ocr_fallback" if reprovacao else "ocr"))
                continue

            if pytesseract is None:
                raise RuntimeError("pagina %d precisa de OCR%s: %s" % (
                    indice + 1, " (%s)" % reprovacao if reprovacao else "", motivo_sem_ocr))

            if verbose:
                print("      - OCR da pagina %d/%d%s..." % (
                    indice + 1, total,
                    " (camada textual reprovada: %s)" % reprovacao if reprovacao else ""))
            leitura = ocr_com_reforco(pagina, pytesseract, dpi=dpi, verbose=verbose)

            paginas.append({
                "numero": indice + 1,
                "metodo": "ocr_fallback" if reprovacao else "ocr",
                "texto": leitura["texto"],
                "conf": leitura["conf"],
                "dpi": leitura["dpi"],
                "rotacao": leitura["rotacao"],
            })
            gc.collect()

            ocr_desde_salvo += 1
            if caminho_parcial and ocr_desde_salvo >= SALVAR_A_CADA:
                _salvar_parcial(caminho_parcial, hash_pdf, paginas)
                ocr_desde_salvo = 0

    extracao = resumo_extracao(paginas)
    extracao["sha256"] = hash_pdf
    return extracao


# ---------------------------------------------------------------------------
# Etapa 2 - Limpeza
# ---------------------------------------------------------------------------

# Blocos de assinatura digital / validacao de documento do STF e do PJe.
PADROES_ASSINATURA = [
    (r"documento assinado digitalmente", "assinatura_digital"),
    (r"assinado eletronicamente por", "assinatura_digital"),
    (r"medida provis[oó]ria n[.º°]*\s*2\.?200-2", "assinatura_digital"),
    (r"mp n[.º°]*\s*2\.?200-2", "assinatura_digital"),
    (r"autenticidade d[eo]st[ea]\s+documento", "validacao"),
    (r"pode ser (conferida|verificada|confirmada)", "validacao"),
    (r"c[oó]digo (verificador|de validac|localizador)", "validacao"),
    (r"chave de acesso", "validacao"),
    (r"impresso por", "carimbo"),
    (r"inteiro teor do ac[oó]rd[aã]o", "carimbo"),
    (r"^supremo tribunal federal$", "carimbo"),
    (r"^stf$", "carimbo"),
]

# Linha que e so numeracao de pagina.
PADRAO_NUM_PAGINA = re.compile(
    r"^\s*(?:-\s*)?(?:p[aá]g(?:\.|ina)?\s*)?\d{1,4}\s*(?:/|de)?\s*\d{0,4}\s*(?:-\s*)?$",
    re.IGNORECASE,
)

# URL de autenticacao (so removida quando aparece isolada numa linha).
PADRAO_URL_STF = re.compile(
    r"^\s*(?:https?://)?(?:www\.)?\S*(?:stf\.jus\.br|pje\.jus\.br)\S*\s*$",
    re.IGNORECASE,
)

# Inicio de item/lista/dispositivo: nunca juntar com a linha anterior.
PADRAO_INICIO_ITEM = re.compile(
    r"^\s*(?:[-*\u2022\u25cf]|\d{1,3}[.)\u2013-]|[a-z]\)|[IVXLC]{1,6}\s*[-.)]|"
    r"art\.|§|par[aá]grafo|inciso|al[ií]nea|\([a-z0-9ivx]{1,4}\))",
    re.IGNORECASE,
)

LIGADURAS = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u00a0": " ",
}


def _normaliza_para_comparar(linha):
    """Versao canonica da linha, usada so para detectar repeticao entre paginas.

    Numeros viram '#' para que 'Pagina 3 de 40' e 'Pagina 7 de 40' contem como
    a mesma linha de rodape.
    """
    texto = unicodedata.normalize("NFKD", linha.strip().lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"\d+", "#", texto)
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


# Quantos caracteres do inicio/fim da linha formam a chave "tolerante a OCR".
# Curto de proposito: no timbre escaneado o OCR erra varios caracteres por
# linha, e uma chave longa deixa de casar. O risco de a chave curta pegar texto
# de verdade e contido pelas duas exigencias da regra: estar na borda da pagina
# e se repetir em mais de FRACAO_REPETICAO das paginas.
CHAVE_BOILERPLATE = 15


def _chaves_tolerantes(normalizada):
    """Chaves de inicio e de fim da linha, para casar apesar do ruido do OCR.

    Numa linha de timbre o erro de reconhecimento costuma cair numa das pontas
    (o logotipo vira 'SE. |' numa pagina, 'MV |' na outra, 'Ye' na seguinte),
    enquanto a outra ponta fica estavel. Guardando as duas pontas, basta uma
    delas se repetir para a linha ser reconhecida como carimbo.
    """
    if len(normalizada) < CHAVE_BOILERPLATE:
        return ()
    return (
        "inicio:" + normalizada[:CHAVE_BOILERPLATE],
        "fim:" + normalizada[-CHAVE_BOILERPLATE:],
    )


def detectar_boilerplate(paginas):
    """Descobre as linhas de topo/rodape que se repetem pagina apos pagina.

    O carimbo do STF (nome do tribunal, numero do documento, data/hora, pagina
    X de Y) e o timbre do escritorio aparecem em toda pagina e viram ruido na
    analise. Em vez de uma lista fixa de regras, a repeticao e medida no proprio
    documento.

    A repeticao e medida DENTRO DO TRECHO em que a linha aparece, e nao no
    documento inteiro. O PDF da peticao inicial costuma trazer a peticao (com
    timbre em toda pagina) seguida de anexos (leis, documentos) sem timbre; num
    caso real o timbre estava em 80 de 275 paginas, 29% do documento, mas em
    praticamente todas as paginas da peticao em si. Uma linha e carimbo quando
    aparece na borda de pelo menos MIN_PAGINAS_BOILERPLATE paginas e em pelo
    menos FRACAO_REPETICAO das paginas entre a primeira e a ultima ocorrencia.
    Uma linha legitima que se repete por acaso aparece espalhada, com densidade
    baixa no proprio trecho.

    Devolve (linhas_exatas, pontas). O casamento por ponta existe porque no OCR
    a mesma linha de timbre sai levemente diferente a cada pagina, e um unico
    erro de reconhecimento ja quebraria a comparacao exata.
    """
    exatas = defaultdict(set)
    pontas = defaultdict(set)
    for indice, pagina in enumerate(paginas):
        linhas = [l for l in pagina["texto"].splitlines() if l.strip()]
        borda = linhas[:LINHAS_BORDA] + linhas[-LINHAS_BORDA:]
        normalizadas = set(_normaliza_para_comparar(l) for l in borda)
        for linha in normalizadas:
            if len(linha) >= 3:
                exatas[linha].add(indice)
            for chave in _chaves_tolerantes(linha):
                pontas[chave].add(indice)

    def e_carimbo(indices):
        if len(indices) < MIN_PAGINAS_BOILERPLATE:
            return False
        extensao = max(indices) - min(indices) + 1
        return len(indices) / extensao >= FRACAO_REPETICAO

    return (
        {linha for linha, indices in exatas.items() if e_carimbo(indices)},
        {chave for chave, indices in pontas.items() if e_carimbo(indices)},
    )


def _e_assinatura(linha):
    """Devolve o rotulo da regra que casou com a linha, ou None."""
    alvo = _normaliza_para_comparar(linha)
    for padrao, rotulo in PADROES_ASSINATURA:
        if re.search(padrao, alvo, re.IGNORECASE):
            return rotulo
    return None


def limpar_paginas(paginas):
    """Remove ruido estrutural e reagrupa o texto de cada pagina.

    Devolve (textos, relatorio): textos[i] e o texto limpo de paginas[i], para
    manter o vinculo com a pagina original (o JSON limpo tem uma entrada por
    pagina, inclusive as que ficam vazias).

    Nada de conteudo argumentativo e removido: as regras miram carimbo de
    cabecalho/rodape, bloco de assinatura digital, numeracao solta de pagina e
    quebras de linha artificiais vindas do layout do PDF.
    """
    boilerplate_exato, boilerplate_pontas = detectar_boilerplate(paginas)
    relatorio = Counter()
    textos = []
    # Chaves de carimbo ja vistas: a primeira ocorrencia de cada carimbo fica no
    # texto e so as repeticoes saem. Assim a limpeza nunca apaga um texto que nao
    # exista em outro ponto do documento - nos anexos, um cabecalho corrido como
    # 'Lei n 13.757' tem na primeira ocorrencia o titulo da lei atacada.
    carimbos_vistos = set()

    for pagina in paginas:
        linhas = pagina["texto"].splitlines()
        # A posicao de borda e contada so entre as linhas com conteudo, igual ao
        # que detectar_boilerplate faz.
        indices_uteis = [i for i, l in enumerate(linhas) if l.strip()]
        total_linhas = len(indices_uteis)
        bordas = set(indices_uteis[:LINHAS_BORDA] + indices_uteis[-LINHAS_BORDA:])
        mantidas = []

        for posicao, linha in enumerate(linhas):
            if not linha.strip():
                mantidas.append("")
                continue

            na_borda = posicao in bordas
            normalizada = _normaliza_para_comparar(linha)

            if na_borda:
                chaves = {normalizada} | set(_chaves_tolerantes(normalizada))
                e_carimbo = (normalizada in boilerplate_exato
                             or bool(chaves & boilerplate_pontas))
                if e_carimbo:
                    if chaves & carimbos_vistos:
                        relatorio["cabecalho_rodape"] += 1
                        continue
                    carimbos_vistos.update(chaves)

            rotulo = _e_assinatura(linha)
            if rotulo:
                relatorio[rotulo] += 1
                continue

            # So na borda: no meio da pagina uma linha com apenas um numero e
            # conteudo, nao paginacao. O layout do PDF quebra '(doc. 21 a 23)'
            # em linhas soltas, e remover o '21' mutilaria a referencia.
            if na_borda and PADRAO_NUM_PAGINA.match(linha):
                relatorio["numero_pagina"] += 1
                continue

            # URL do STF so e ruido quando esta sozinha na linha e perto da borda
            # (bloco de validacao). No meio do texto pode ser citacao.
            if na_borda and PADRAO_URL_STF.match(linha):
                relatorio["validacao"] += 1
                continue

            mantidas.append(linha)

        relatorio["linhas_originais"] += total_linhas
        textos.append("\n".join(mantidas).strip())

    # O vocabulario da hifenizacao e o do documento inteiro: 'consti-tucional'
    # numa pagina e juntado se 'constitucional' aparece em qualquer outra.
    vocabulario = Counter(p.lower() for t in textos for p in re.findall(r"\w+", t))
    limpos = []
    for texto in textos:
        texto, n_unidas, n_mantidas = _juntar_hifenizacao(texto, vocabulario)
        relatorio["hifenizacao_unida"] += n_unidas
        relatorio["hifen_mantido"] += n_mantidas
        limpos.append(_normalizar(_unir_linhas(texto)))
    return limpos, relatorio


# Pronomes que se ligam ao verbo por hifen (trata-se, aplica-lhe, torna-o):
# um hifen antes deles no fim da linha e sempre hifen de verdade.
CLITICOS = {
    "se", "me", "te", "nos", "vos", "lhe", "lhes", "o", "a", "os", "as",
    "lo", "la", "los", "las", "no", "na", "nas", "mo", "ma", "to", "ta",
}


# Itens do relatorio de limpeza que nao sao linhas removidas.
CONTADORES_SEM_REMOCAO = ("linhas_originais", "hifenizacao_unida", "hifen_mantido")


def _juntar_hifenizacao(texto, vocabulario=None):
    """Trata o hifen no fim da linha: quebra de palavra ou hifen de verdade.

    O layout quebra 'constitucional' em 'consti-' / 'tucional', e isso deve
    virar uma palavra so. Mas em portugues muito hifen no fim da linha e
    legitimo: 'trata-' / 'se', 'poluidor-' / 'pagador'. Junta-los produziria
    'tratase' e 'poluidorpagador'. Por isso:

    - antes de pronome (se, lhe, o...) o hifen e mantido;
    - a juncao so e feita se a palavra junta aparece em outro ponto do
      proprio documento (prova de que e uma palavra, e nao duas);
    - na duvida o hifen e mantido e so a quebra de linha sai. 'consti-tucional'
      atrapalha uma busca, mas nao perde informacao; 'poluidorpagador' perde.

    Devolve (texto, n_unidas, n_mantidas).
    """
    # Segunda parte comeca com letra de qualquer caixa: ementas e titulos em
    # maiusculas tambem quebram ('IN-' / 'VESTIDURA').
    padrao = re.compile(r"(\w{2,})-[ \t]*\n[ \t]*([^\W\d_]\w*)")
    if vocabulario is None:
        vocabulario = Counter(p.lower() for p in re.findall(r"\w+", texto))
    contagem = Counter()

    def resolver(m):
        antes, depois = m.group(1), m.group(2)
        if depois.lower() not in CLITICOS and vocabulario[(antes + depois).lower()] > 0:
            contagem["unidas"] += 1
            return antes + depois
        contagem["mantidas"] += 1
        return antes + "-" + depois

    texto = padrao.sub(resolver, texto)
    return texto, contagem["unidas"], contagem["mantidas"]


def _e_titulo(linha):
    """Linha toda em maiusculas: titulo de secao ou enderecamento da peticao."""
    letras = [c for c in linha if c.isalpha()]
    return len(letras) >= 3 and all(c.isupper() for c in letras)


def _unir_linhas(bloco):
    """Desfaz as quebras de linha do layout, preservando paragrafos e listas.

    O PDF quebra a linha a cada linha visual. Sem isso, um paragrafo vira 8
    linhas soltas e qualquer analise por sentenca sai errada. Duas linhas so
    sao unidas quando a primeira nao termina em pontuacao final e a segunda nao
    comeca um item de lista/dispositivo legal.
    """
    linhas = bloco.splitlines()
    resultado = []
    for linha in linhas:
        atual = linha.rstrip()
        if not atual.strip():
            resultado.append("")
            continue
        # Titulo ('DOS FATOS', 'EXCELENTISSIMO SENHOR MINISTRO') nao termina em
        # pontuacao e seria colado no paragrafo seguinte. Duas linhas de titulo
        # seguidas continuam sendo unidas (titulo quebrado em duas linhas).
        if (
            resultado
            and resultado[-1].strip()
            and not re.search(r"[.!?:;]\s*$", resultado[-1])
            and not PADRAO_INICIO_ITEM.match(atual)
            and _e_titulo(resultado[-1]) == _e_titulo(atual)
        ):
            resultado[-1] = resultado[-1] + " " + atual.strip()
        else:
            resultado.append(atual)
    return "\n".join(resultado)


def _normalizar(texto):
    """Unicode, ligaduras, espacos e linhas em branco excedentes.

    NFC e nao NFKC: o NFKC 'simplifica' o indicador ordinal, e 'nº' vira
    'no', '2º' vira '2o', '§ 1º' vira '§ 1o' - informacao juridica perdida.
    As ligaduras tipograficas, que eram o motivo para usar NFKC, ja sao
    trocadas explicitamente pela tabela LIGADURAS.
    """
    texto = unicodedata.normalize("NFC", texto)
    for origem, destino in LIGADURAS.items():
        texto = texto.replace(origem, destino)
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r" *\n *", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


# ---------------------------------------------------------------------------
# Etapa 3 - Escrita: JSON por pagina, documentos.csv e log
# ---------------------------------------------------------------------------

def resumo_extracao(paginas):
    """Monta o dict de extracao (contagens, metodo, confianca media) das paginas."""
    # 'ocr' e 'ocr_fallback' contam como OCR; o fallback e informado a parte.
    n_ocr = sum(1 for p in paginas if p["metodo"].startswith("ocr"))
    n_fallback = sum(1 for p in paginas if p["metodo"] == "ocr_fallback")
    n_digital = len(paginas) - n_ocr
    confs = [p["conf"] for p in paginas if p["conf"] is not None]
    return {
        "paginas": paginas,
        "total_paginas": len(paginas),
        "paginas_digital": n_digital,
        "paginas_ocr": n_ocr,
        "paginas_ocr_fallback": n_fallback,
        "metodo": "digital" if n_ocr == 0 else ("ocr" if n_digital == 0 else "misto"),
        "conf_ocr_media": round(sum(confs) / len(confs), 1) if confs else None,
    }


def _agora():
    return _dt.datetime.now().isoformat(timespec="seconds")


def _pagina_json(pagina, texto):
    """Pagina no formato dos JSON: 'page' e 'text' mais os dados da leitura."""
    return {
        "page": pagina["numero"],
        "metodo": pagina["metodo"],
        "conf": pagina.get("conf"),
        "dpi": pagina.get("dpi"),
        "rotacao": pagina.get("rotacao") or 0,
        "text": texto,
    }


def _pagina_interna(pagina_json):
    """Inverso de _pagina_json: volta ao formato usado pela extracao e limpeza."""
    return {
        "numero": pagina_json["page"],
        "metodo": pagina_json["metodo"],
        "texto": pagina_json["text"],
        "conf": pagina_json.get("conf"),
        "dpi": pagina_json.get("dpi"),
        "rotacao": pagina_json.get("rotacao") or 0,
    }


def _resumo_json(extracao):
    return {
        "paginas": extracao["total_paginas"],
        "paginas_digital": extracao["paginas_digital"],
        "paginas_ocr": extracao["paginas_ocr"],
        "paginas_ocr_fallback": extracao["paginas_ocr_fallback"],
        "metodo": extracao["metodo"],
        "conf_ocr_media": extracao["conf_ocr_media"],
    }


def gravar_json(caminho, dados):
    """Escrita atomica: um JSON pela metade nunca fica no lugar do anterior."""
    caminho = Path(caminho)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporario, caminho)


def montar_json_ocr(doc_id, extracao):
    """Texto de cada pagina exatamente como saiu da extracao, sem limpeza."""
    return {
        "id": doc_id,
        "arquivo_pdf": doc_id + ".pdf",
        "sha256": extracao.get("sha256"),
        "etapa": "ocr",
        "versao_pipeline": VERSAO_PIPELINE,
        "processado_em": _agora(),
        "resumo": _resumo_json(extracao),
        "pages": [_pagina_json(p, p["texto"].strip()) for p in extracao["paginas"]],
    }


def montar_json_clean(doc_id, extracao, textos_limpos, relatorio):
    """Mesma estrutura do JSON de OCR, com o texto limpo de cada pagina."""
    chars_ocr = sum(len(p["texto"].strip()) for p in extracao["paginas"])
    chars_clean = sum(len(t) for t in textos_limpos)
    resumo = _resumo_json(extracao)
    resumo.update({
        "chars_ocr": chars_ocr,
        "chars_clean": chars_clean,
        "reducao_pct": round(100 * (1 - chars_clean / chars_ocr), 1) if chars_ocr else 0,
        "linhas_removidas": sum(v for k, v in relatorio.items()
                                if k not in CONTADORES_SEM_REMOCAO),
        # Quantas linhas cada regra tirou, para auditar a limpeza.
        "limpeza_detalhe": {k: v for k, v in sorted(relatorio.items())
                            if k != "linhas_originais"},
    })
    return {
        "id": doc_id,
        "arquivo_pdf": doc_id + ".pdf",
        "sha256": extracao.get("sha256"),
        "etapa": "clean",
        "versao_pipeline": VERSAO_PIPELINE,
        "processado_em": _agora(),
        "resumo": resumo,
        "pages": [_pagina_json(p, t) for p, t in zip(extracao["paginas"], textos_limpos)],
    }


def ler_json_ocr(caminho):
    """Reconstroi a extracao a partir de data/02_processed/ocr/<ID>.json.

    Permite refazer a limpeza sem repetir o OCR, que e a parte cara do pipeline.
    """
    dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    extracao = resumo_extracao([_pagina_interna(p) for p in dados["pages"]])
    extracao["sha256"] = dados.get("sha256")
    return extracao


def ocr_anterior(caminho_json, hash_pdf):
    """Paginas lidas por OCR numa extracao anterior do mesmo PDF ({numero: pagina}).

    So vale se o hash bater: se o PDF mudou, as paginas antigas nao servem.
    """
    try:
        dados = json.loads(Path(caminho_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if dados.get("sha256") != hash_pdf:
        return {}
    return {p["page"]: _pagina_interna(p) for p in dados["pages"]
            if p["metodo"].startswith("ocr")}


# Colunas do metadata/documentos.csv que o pipeline preenche. As demais (relator,
# assuntos, sha256...) sao do scripts/00_registrar_documentos.py e nao sao tocadas.
COLUNAS_PROCESSAMENTO = [
    "status_ocr", "metodo", "paginas_digital", "paginas_ocr", "paginas_ocr_fallback",
    "conf_ocr_media", "chars_ocr", "chars_clean", "reducao_pct", "linhas_removidas",
    "erro", "processado_em", "versao_pipeline",
]

COLUNAS_LOG = ["data_hora", "documento", "etapa", "status", "segundos", "erro",
               "versao_pipeline"]


def atualizar_documentos(registros, caminho=None):
    """Grava as colunas de processamento dos documentos no documentos.csv."""
    caminho = Path(caminho or CAMINHO_DOCUMENTOS)
    if caminho.exists():
        df = pd.read_csv(caminho, dtype=str, keep_default_na=False)
    else:
        df = pd.DataFrame(columns=["id"])
    for coluna in COLUNAS_PROCESSAMENTO:
        if coluna not in df.columns:
            df[coluna] = ""

    for r in registros:
        valores = {c: "" if r.get(c) is None else str(r.get(c)) for c in COLUNAS_PROCESSAMENTO}
        linha = df["id"] == r["id"]
        if linha.any():
            df.loc[linha, list(valores)] = list(valores.values())
        else:
            print("  ! %s nao esta no documentos.csv (rode scripts/00_registrar_documentos.py)"
                  % r["id"])
            df = pd.concat([df, pd.DataFrame([{"id": r["id"], **valores}])], ignore_index=True)

    caminho.parent.mkdir(parents=True, exist_ok=True)
    df.fillna("").sort_values("id").to_csv(caminho, index=False, encoding="utf-8-sig")


def registrar_log(entradas, caminho=None):
    """Acrescenta linhas ao logs/processing.csv (nunca apaga as anteriores)."""
    if not entradas:
        return
    caminho = Path(caminho or CAMINHO_LOG)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    novo = not caminho.exists()
    pd.DataFrame(entradas).reindex(columns=COLUNAS_LOG).to_csv(
        caminho, mode="a", header=novo, index=False,
        encoding="utf-8-sig" if novo else "utf-8")


def _entrada_log(doc_id, etapa, status, inicio, erro=""):
    return {"data_hora": _agora(), "documento": doc_id, "etapa": etapa, "status": status,
            "segundos": round(time.time() - inicio, 1), "erro": erro,
            "versao_pipeline": VERSAO_PIPELINE}


# ---------------------------------------------------------------------------
# Orquestracao
# ---------------------------------------------------------------------------

def _limpar_e_gravar(doc_id, extracao, saida, verbose=True):
    """Limpa, grava clean/<ID>.json e devolve as colunas para o documentos.csv."""
    textos, relatorio = limpar_paginas(extracao["paginas"])
    dados = montar_json_clean(doc_id, extracao, textos, relatorio)
    gravar_json(Path(saida) / "clean" / (doc_id + ".json"), dados)

    r = dados["resumo"]
    if verbose:
        print("    -> %s | %d pag (%d digital / %d ocr) | %d -> %d chars" % (
            r["metodo"], r["paginas"], r["paginas_digital"], r["paginas_ocr"],
            r["chars_ocr"], r["chars_clean"]))
    return {k: r[k] for k in ("metodo", "paginas", "paginas_digital", "paginas_ocr",
                              "paginas_ocr_fallback", "conf_ocr_media", "chars_ocr",
                              "chars_clean", "reducao_pct", "linhas_removidas")}


def _novo_registro(doc_id):
    return {"id": doc_id, "status_ocr": "OK", "erro": "", "processado_em": _agora(),
            "versao_pipeline": VERSAO_PIPELINE, "log": []}


def _processar_documento(caminho, saida, dpi, refazer, pytesseract, motivo_sem_ocr,
                         verbose=True):
    """Extrai, grava ocr/<ID>.json, limpa e grava clean/<ID>.json de um PDF.

    Devolve o registro (colunas do documentos.csv + entradas de log). Nunca
    levanta excecao: falha vira status PENDENTE_OCR ou ERRO, para um PDF ruim
    nao derrubar a rodada inteira.
    """
    caminho, saida = Path(caminho), Path(saida)
    doc_id = caminho.stem
    registro = _novo_registro(doc_id)
    json_ocr = saida / "ocr" / (doc_id + ".json")

    # Progresso parcial do OCR; --refazer descarta o de rodadas anteriores.
    parcial = saida / "ocr" / ".parcial" / (doc_id + ".json")
    if refazer and parcial.exists():
        parcial.unlink()

    inicio = time.time()
    try:
        hash_pdf = sha256_arquivo(caminho)
        # Sem --refazer, paginas ja lidas por OCR deste mesmo PDF sao
        # reaproveitadas: so as paginas digitais sao refeitas.
        anteriores = {} if refazer else ocr_anterior(json_ocr, hash_pdf)
        if verbose and anteriores:
            print("      reaproveitando o OCR de %d pagina(s) ja lidas" % len(anteriores))
        extracao = extrair_documento(
            caminho, pytesseract, dpi=dpi, verbose=verbose,
            motivo_sem_ocr=motivo_sem_ocr, caminho_parcial=parcial,
            paginas_ocr_anteriores=anteriores, hash_pdf=hash_pdf,
        )
        gravar_json(json_ocr, montar_json_ocr(doc_id, extracao))
        if parcial.exists():
            parcial.unlink()
        registro["log"].append(_entrada_log(doc_id, "ocr", "OK", inicio))
    except RuntimeError as e:
        registro.update(status_ocr="PENDENTE_OCR", erro=str(e))
        registro["log"].append(_entrada_log(doc_id, "ocr", "PENDENTE_OCR", inicio, str(e)))
        if verbose:
            print("    -> PENDENTE: %s" % e)
        return registro
    except Exception as e:
        erro = "%s: %s" % (type(e).__name__, e)
        registro.update(status_ocr="ERRO", erro=erro)
        registro["log"].append(_entrada_log(doc_id, "ocr", "ERRO", inicio, erro))
        if verbose:
            print("    -> ERRO: %s" % erro)
        return registro

    inicio = time.time()
    try:
        registro.update(_limpar_e_gravar(doc_id, extracao, saida, verbose))
        registro["log"].append(_entrada_log(doc_id, "clean", "OK", inicio))
    except Exception as e:
        erro = "limpeza: %s: %s" % (type(e).__name__, e)
        registro.update(status_ocr="ERRO", erro=erro)
        registro["log"].append(_entrada_log(doc_id, "clean", "ERRO", inicio, erro))
        if verbose:
            print("    -> ERRO: %s" % erro)
    gc.collect()
    return registro


def _registrar(registros):
    """Grava documentos.csv e log de uma vez (so o processo principal escreve)."""
    atualizar_documentos(registros)
    registrar_log([e for r in registros for e in r.get("log", [])])


def reler_paginas_baixas(saida=DIR_PROCESSADO, entrada=DIR_PDF, caminho_tesseract=None,
                         verbose=True):
    """Rele, com correcao de rotacao, so as paginas de confianca baixa ja extraidas.

    Serve para aplicar a deteccao de pagina girada a documentos processados
    antes dela existir, sem refazer o OCR inteiro (no ADPF_165 seriam 1h30
    para recuperar umas poucas paginas). As tentativas 1 e 2 (DPI normal e
    reforco) ja foram feitas nessas paginas, entao so a rotacao e testada.
    """
    saida, entrada = Path(saida), Path(entrada)
    pytesseract, msg = configurar_tesseract(caminho_tesseract)
    if verbose:
        print(msg)
    if pytesseract is None:
        return pd.DataFrame()

    registros = []
    for caminho_json in sorted((saida / "ocr").glob("*.json")):
        doc_id = caminho_json.stem
        pdf = entrada / (doc_id + ".pdf")
        extracao = ler_json_ocr(caminho_json)
        alvo = [p for p in extracao["paginas"]
                if p["metodo"].startswith("ocr") and p["conf"] is not None
                and p["conf"] < CONF_MINIMA and not p.get("rotacao")]
        if not alvo or not pdf.exists():
            continue
        if extracao.get("sha256") and sha256_arquivo(pdf) != extracao["sha256"]:
            print("  ! %s: o PDF mudou desde a extracao; rode sem --reler-baixa" % doc_id)
            continue
        if verbose:
            print("  - %s: %d pagina(s) com confianca baixa" % (doc_id, len(alvo)))

        inicio = time.time()
        recuperadas = 0
        with fitz.open(pdf) as doc:
            for p in alvo:
                antes = {"texto": p["texto"], "conf": p["conf"], "dpi": p["dpi"], "rotacao": 0}
                depois = tentar_rotacao(doc[p["numero"] - 1], pytesseract, antes)
                if depois is not antes:
                    if verbose:
                        print("      pagina %d: girada %d graus, conf %.1f -> %.1f"
                              % (p["numero"], depois["rotacao"], antes["conf"], depois["conf"]))
                    p.update(depois)
                    recuperadas += 1
                gc.collect()

        if verbose:
            print("    -> %d de %d recuperada(s)" % (recuperadas, len(alvo)))
        if not recuperadas:
            continue

        hash_pdf = extracao.get("sha256")
        extracao = resumo_extracao(extracao["paginas"])
        extracao["sha256"] = hash_pdf
        gravar_json(caminho_json, montar_json_ocr(doc_id, extracao))
        registro = _novo_registro(doc_id)
        registro["log"].append(_entrada_log(doc_id, "reler_baixa", "OK", inicio))
        inicio = time.time()
        registro.update(_limpar_e_gravar(doc_id, extracao, saida, verbose))
        registro["log"].append(_entrada_log(doc_id, "clean", "OK", inicio))
        registros.append(registro)

    _registrar(registros)
    return pd.DataFrame(registros).drop(columns="log", errors="ignore")


def relimpar_pasta(saida=DIR_PROCESSADO, verbose=True):
    """Refaz so a limpeza, a partir de ocr/*.json, sem repetir o OCR.

    Usado para ajustar as regras de limpeza: o OCR de uma base grande leva horas,
    e o texto de cada pagina ja esta gravado.
    """
    saida = Path(saida)
    jsons = sorted((saida / "ocr").glob("*.json"))
    if verbose:
        print("Refazendo a limpeza de %d documento(s) de %s\n" % (len(jsons), saida / "ocr"))

    registros = []
    for caminho in jsons:
        doc_id = caminho.stem
        if verbose:
            print("  - %s" % doc_id)
        registro = _novo_registro(doc_id)
        inicio = time.time()
        try:
            registro.update(_limpar_e_gravar(doc_id, ler_json_ocr(caminho), saida, verbose))
            registro["log"].append(_entrada_log(doc_id, "clean", "OK", inicio))
        except Exception as e:
            erro = "limpeza: %s: %s" % (type(e).__name__, e)
            registro.update(status_ocr="ERRO", erro=erro)
            registro["log"].append(_entrada_log(doc_id, "clean", "ERRO", inicio, erro))
            if verbose:
                print("    -> ERRO: %s" % erro)
        registros.append(registro)

    _registrar(registros)
    return pd.DataFrame(registros).drop(columns="log", errors="ignore")


def _preparar_ocr(caminho_tesseract, so_digital):
    """(pytesseract, mensagem): pytesseract e None quando o OCR nao vai rodar."""
    if so_digital:
        return None, "OCR desligado por --so-digital"
    return configurar_tesseract(caminho_tesseract)


# Estado de cada processo worker: o modulo pytesseract nao pode ser enviado
# entre processos, entao cada worker localiza o Tesseract uma vez ao iniciar.
_OCR_DO_WORKER = (None, "")


def _iniciar_worker(caminho_tesseract, so_digital):
    global _OCR_DO_WORKER
    # Cada Tesseract com uma thread so: com varios PDFs em paralelo, deixar cada
    # um abrir varias threads disputa a CPU e fica mais lento que sequencial.
    os.environ["OMP_THREAD_LIMIT"] = "1"
    _OCR_DO_WORKER = _preparar_ocr(caminho_tesseract, so_digital)


def _processar_no_worker(caminho, saida, dpi, refazer):
    pytesseract, msg = _OCR_DO_WORKER
    return _processar_documento(caminho, saida, dpi, refazer, pytesseract, msg,
                                verbose=False)


def _resumo_registro(r):
    if r["status_ocr"] != "OK":
        return "%s: %s" % (r["status_ocr"], r["erro"])
    segundos = sum(e["segundos"] for e in r["log"])
    return "%s | %s pag (%s digital / %s ocr) | %.1fs" % (
        r["metodo"], r["paginas"], r["paginas_digital"], r["paginas_ocr"], segundos)


def processar_pasta(entrada=DIR_PDF, saida=DIR_PROCESSADO, dpi=DPI_PADRAO, refazer=False,
                    caminho_tesseract=None, so_digital=False, workers=1, verbose=True):
    """Processa todos os PDFs de uma pasta e devolve um DataFrame com o resultado.

    O ID de cada documento e o nome do PDF (data/01_raw/pdf/ADI_6002.pdf ->
    ADI_6002). Um documento que ja tem clean/<ID>.json e pulado, a menos que se
    peca refazer=True (que refaz tudo, inclusive o OCR).

    Com so_digital=True o OCR nao e acionado: os documentos com camada textual
    saem prontos em segundos e os escaneados ficam com status PENDENTE_OCR, para
    uma rodada dedicada depois.

    Com workers > 1, varios PDFs sao processados ao mesmo tempo, um por processo
    (o OCR e limitado por CPU). Os maiores PDFs entram primeiro, para um
    escaneado de mil paginas nao ficar sozinho no fim da rodada. Nesse modo o
    log mostra uma linha por documento, em vez do progresso pagina a pagina.
    """
    entrada = Path(entrada)
    saida = Path(saida)

    pytesseract, msg = _preparar_ocr(caminho_tesseract, so_digital)
    if verbose:
        print(msg if pytesseract else "AVISO: %s" % msg)
        print("      (arquivos que precisarem de OCR ficarao com status PENDENTE_OCR)"
              if not pytesseract else "")

    pdfs = sorted(p for p in entrada.glob("*.pdf"))
    if verbose:
        print("\n%d PDF(s) em %s\n" % (len(pdfs), entrada))

    pendentes = []
    for caminho in pdfs:
        if (saida / "clean" / (caminho.stem + ".json")).exists() and not refazer:
            if verbose:
                print("  - %s: ja processado, pulando (use --refazer)" % caminho.stem)
            continue
        pendentes.append(caminho)

    registros = []

    def registrar(registro):
        registros.append(registro)
        # Grava a cada documento: se a rodada cair, o que ja terminou fica
        # registrado no documentos.csv e no log.
        _registrar([registro])

    if workers <= 1 or len(pendentes) <= 1:
        for caminho in pendentes:
            if verbose:
                print("  - %s" % caminho.stem)
            registrar(_processar_documento(caminho, saida, dpi, refazer,
                                           pytesseract, msg, verbose))
    else:
        pendentes.sort(key=lambda p: p.stat().st_size, reverse=True)
        if verbose:
            print("Processando %d PDF(s) com %d workers...\n" % (len(pendentes), workers))
        with ProcessPoolExecutor(max_workers=workers, initializer=_iniciar_worker,
                                 initargs=(caminho_tesseract, so_digital)) as executor:
            futuros = {
                executor.submit(_processar_no_worker, caminho, saida, dpi, refazer): caminho
                for caminho in pendentes
            }
            for k, futuro in enumerate(as_completed(futuros), 1):
                caminho = futuros[futuro]
                try:
                    registro = futuro.result()
                except Exception as e:
                    # So chega aqui se o processo worker morreu (ex.: falta de
                    # memoria); erros normais ja viram status dentro dele.
                    erro = "%s: %s" % (type(e).__name__, e)
                    registro = _novo_registro(caminho.stem)
                    registro.update(status_ocr="ERRO", erro=erro)
                    registro["log"].append(_entrada_log(caminho.stem, "ocr", "ERRO",
                                                        time.time(), erro))
                registrar(registro)
                if verbose:
                    print("  [%d/%d] %s -> %s" % (k, len(pendentes), caminho.stem,
                                                  _resumo_registro(registro)))

    if verbose and registros:
        print("\nResultados em %s (ocr/ e clean/)" % saida)
        print("Status em %s; log em %s" % (CAMINHO_DOCUMENTOS, CAMINHO_LOG))
    return pd.DataFrame(registros).drop(columns="log", errors="ignore")


def main():
    parser = argparse.ArgumentParser(
        description="Extracao de texto (digital + OCR) e limpeza das peticoes iniciais."
    )
    parser.add_argument("--entrada", default=str(DIR_PDF),
                        help="pasta com os PDFs nomeados pelo ID (padrao: data/01_raw/pdf)")
    parser.add_argument("--saida", default=str(DIR_PROCESSADO),
                        help="pasta com ocr/ e clean/ (padrao: data/02_processed)")
    parser.add_argument("--dpi", type=int, default=DPI_PADRAO,
                        help="DPI de renderizacao para OCR (padrao: %d)" % DPI_PADRAO)
    parser.add_argument("--refazer", action="store_true",
                        help="reprocessa tudo, inclusive o OCR das paginas ja lidas")
    parser.add_argument("--so-digital", action="store_true", dest="so_digital",
                        help="nao roda OCR; processa so o que tem camada textual")
    parser.add_argument("--reler-baixa", action="store_true", dest="reler_baixa",
                        help="rele, corrigindo rotacao, so as paginas de confianca baixa "
                             "ja extraidas (sem refazer o OCR inteiro)")
    parser.add_argument("--relimpar", action="store_true",
                        help="refaz so a limpeza a partir de ocr/*.json, sem repetir o OCR")
    parser.add_argument("--tesseract", default=None,
                        help="caminho do tesseract.exe, se nao estiver no PATH")
    parser.add_argument("--workers", type=int, default=1,
                        help="quantos PDFs processar em paralelo (padrao: 1); "
                             "sugestao: numero de nucleos da CPU menos 1")
    args = parser.parse_args()

    if args.reler_baixa:
        reler_paginas_baixas(args.saida, args.entrada, caminho_tesseract=args.tesseract)
    elif args.relimpar:
        relimpar_pasta(args.saida)
    else:
        processar_pasta(args.entrada, args.saida, dpi=args.dpi, refazer=args.refazer,
                        caminho_tesseract=args.tesseract, so_digital=args.so_digital,
                        workers=args.workers)


if __name__ == "__main__":
    main()
