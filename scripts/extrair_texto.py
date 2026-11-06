# -*- coding: utf-8 -*-
"""
Extrai o texto de cada pagina das peticoes iniciais baixadas em data/01_raw/iniciais_stf/.

Para cada PDF, gera um arquivo .jsonl (um JSON por linha) com uma linha por pagina:

    {"pagina": 3, "origem": "ocr", "chars_pdf": 0, "cobertura_imagem": 0.98,
     "texto_visivel": 0.0, "confianca_ocr": 91, "giro": 0,
     "texto": "...", "texto_figuras": ""}

- "origem" diz de onde veio o "texto":
    "nativo"     -> o PDF ja tinha texto de verdade (gerado no Word, por exemplo)
    "ocr"        -> a pagina era imagem; o texto veio do Tesseract
    "ocr_falhou" -> era pra ser OCR, mas o Tesseract deu erro; o texto fica vazio
- "chars_pdf", "cobertura_imagem" e "texto_visivel" sao os tres numeros usados
  pra decidir entre nativo e OCR (ver precisa_ocr). Ficam gravados pra dar pra
  auditar a decisao depois.
- "confianca_ocr": confianca media do Tesseract na pagina (0 a 100), ou null se
  a pagina nao passou pelo OCR. Serve pra achar as paginas ruins depois.
- "giro": quantos graus a pagina foi girada antes do OCR (0, 90, 180 ou 270),
  quando o scan estava de lado ou de cabeca pra baixo (ver ocr_com_giro).
- "texto_figuras": so em pagina nativa. E o texto que esta DENTRO das imagens
  grandes da pagina: tabela colada como imagem, print de noticia, citacao
  escaneada, o timbrado do escritorio. Fica separado de "texto" pra nao
  misturar texto exato (nativo) com texto de OCR. Pra busca, usar os dois.

Entrada : data/01_raw/iniciais_stf/<CLASSE>/<CLASSE>_<NUM>_inicial.pdf
Saida   : data/02_processed/textos_jsonl/<CLASSE>/<CLASSE>_<NUM>_inicial.jsonl
          data/02_processed/textos_jsonl/_log.csv        -> um resumo por PDF (paginas, quantas OCR, tempo)
          data/02_processed/textos_jsonl/_estado.json    -> checkpoint, permite parar e retomar

Os caminhos sao relativos a raiz do projeto, entao da pra rodar de qualquer pasta.

Uso:
    pip install -r requirements.txt
    caffeinate -i python scripts/extrair_texto.py
    (a carga leva ~80 min no M1. Deixe o Mac na tomada: o caffeinate impede
    que ele durma por inatividade, mas nao quando a bateria acaba)

Nao precisa instalar nada no sistema: o pacote tesserocr ja vem com o Tesseract
compilado dentro, e os modelos (portugues + deteccao de orientacao, do
tessdata_best) sao baixados do GitHub do Tesseract na primeira rodada, pra
modelos/tessdata_best/.

Paralelismo: o Tesseract usa um nucleo so. Pra usar o processador todo, o
script abre varios processos (--processos, padrao PROCESSOS), cada um com o
seu Tesseract, e manda as paginas de cada PDF pra eles em paralelo. O processo
principal so junta as paginas na ordem, grava o .jsonl e cuida do checkpoint.

Enquanto roda, o Tesseract as vezes imprime "Error in boxClipToRectangle" e
"Error in pixScanForForeground". Sao avisos internos da biblioteca de imagem
dele (Leptonica); nao derrubam a pagina nem mudam o resultado. Pode ignorar.

Opcoes uteis:
    --pasta data/amostra/amostra_iniciais --saida data/amostra/textos
                        roda na amostra sem misturar com o corpus
    --limite 20         so os 20 primeiros PDFs
    --so-classe ADO     filtra por classe
    --refazer           ignora o checkpoint e reprocessa tudo
    --forcar-ocr ADI_3494_inicial
                        OCR em todas as paginas desse documento (pode repetir a opcao)
    --processos 4       quantos Tesseracts rodando ao mesmo tempo
    --dpi 300           resolucao da imagem mandada pro Tesseract
"""

import argparse
import csv
import hashlib
import io
import json
import os
import signal
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import pymupdf  # le o PDF, extrai o texto nativo e renderiza a pagina
import tesserocr  # o Tesseract em si, embutido no pacote (nao precisa de brew)
from PIL import Image  # o tesserocr recebe a pagina como imagem PIL

# ---------------------------------------------------------------- config

RAIZ = Path(__file__).resolve().parent.parent
ENTRADA = RAIZ / "data" / "01_raw" / "iniciais_stf"
SAIDA = RAIZ / "data" / "02_processed" / "textos_jsonl"

# Uma pagina com menos de 50 caracteres de texto nativo e tratada como
# "sem texto": ou esta em branco, ou e uma imagem. 50 e pouco de proposito --
# uma pagina de peticao tem uns 2.000; so um carimbo ou numero de folha
# fica abaixo disso.
MIN_CHARS = 50

# Se a maior imagem cobre metade da pagina ou mais, a pagina pode ser um scan.
# Na amostra os escaneados dao 0,90-1,00 e os nativos sem timbrado no maximo
# 0,12, entao 0,5 fica bem no meio.
MIN_COBERTURA = 0.5

# Mas imagem de pagina inteira nao quer dizer scan: no corpus, ~2.700 paginas
# sao PDF do Word com o papel timbrado do escritorio como imagem de fundo, e o
# texto nativo por cima e perfeito. O que separa as duas coisas e se o texto
# APARECE: no Word ele e desenhado visivel, por cima da imagem; no OCR antigo
# ele e invisivel (Acrobat) ou fica escondido embaixo da imagem (scanner).
# No levantamento de 06/10/2026 quase toda pagina caiu num extremo (>= 80% ou
# <= 20% visivel), entao 0,5 fica no meio.
MIN_VISIVEL = 0.5

# Numa pagina nativa, imagem que cobre 15% da pagina ou mais passa pelo OCR
# (vai pra "texto_figuras"). Logotipo pequeno fica abaixo disso: 95% das
# paginas nativas tem a maior imagem abaixo de 0,15.
MIN_COBERTURA_FIGURA = 0.15

# 300 dpi e o padrao recomendado pelo Tesseract: abaixo disso ele erra mais
# em letra pequena, acima disso so fica mais lento.
DPI = 300

# Pagina de lado ou de cabeca pra baixo sai com confianca baixa no OCR. Se a
# confianca ficar abaixo de 70, pergunta ao detector de orientacao (OSD) do
# Tesseract se ela esta girada. Mas o OSD erra, as vezes com confianca alta
# (mandou girar 180 graus uma pagina em pe da ADPF 165), entao so fica com a
# versao girada se o OCR dela for pelo menos 10 pontos melhor. No teste com as
# 151 paginas em que o OSD sugeriu giro, isso acertou todas as que olhamos:
# pagina de lado de verdade sai de ~40 pra ~80; pagina em pe cai de ~85 pra ~40.
MIN_CONFIANCA_SEM_GIRO = 70
GANHO_MIN_GIRO = 10

# Quantos processos (Tesseracts) rodam ao mesmo tempo. Medido no M1 (8
# nucleos: 4 rapidos + 4 de economia) numa peticao escaneada de 103 paginas:
# 4 processos 113 s, 6 processos 97 s, 8 processos 91 s. Os nucleos de
# economia quase nao ajudam, e cada processo ocupa ~250 MB de memoria, entao
# o padrao deixa 2 nucleos livres pro processo principal e pro resto do Mac.
PROCESSOS = max(1, (os.cpu_count() or 2) - 2)

# Modelos do Tesseract. O tesserocr traz o programa, mas nao os modelos.
# Usamos o "tessdata_best" (o mais preciso): numa comparacao em 16 paginas
# escaneadas do corpus ele acertou muito mais palavra que o modelo padrao
# (acento que o padrao perdia, "protacolo" que virou "protocolo", "Guando"
# que virou "Quando"), ao custo de ~40% mais tempo por pagina.
# - por.traineddata: portugues
# - osd.traineddata: deteccao de orientacao (pagina de lado / de cabeca pra baixo)
# O link aponta pra um commit fixo do repositorio, e o hash confere o arquivo:
# assim a carga e reproduzivel e um download pela metade nao passa despercebido.
TESSDATA = RAIZ / "modelos" / "tessdata_best"
IDIOMA = "por"
COMMIT_MODELOS = "e12c65a915945e4c28e237a9b52bc4a8f39a0cec"
URL_MODELOS = f"https://github.com/tesseract-ocr/tessdata_best/raw/{COMMIT_MODELOS}/"
MODELOS = {
    "por.traineddata": "711de9dbb8052067bd42f16b9119967f30bada80d57e2ef24f65d09f531adb04",
    "osd.traineddata": "9cf5d576fcc47564f11265841e5ca839001e7e6f38ff7f7aacf46d15a96b00ff",
}

# ---------------------------------------------------------------- decisoes
# Funcoes "puras": recebem numeros/nomes e devolvem uma resposta, sem tocar em
# disco nem no Tesseract. Sao as que tem teste em tests/test_extrair_texto.py.


def precisa_ocr(chars_pdf, cobertura_imagem, texto_visivel):
    """Decide se a pagina vai pro Tesseract ou se o texto do PDF serve.

    Roda OCR quando:
    - a pagina quase nao tem texto (em branco, ou imagem pura). OCR numa
      pagina em branco devolve vazio, entao nao custa nada; ou
    - a pagina e basicamente uma imagem E o texto que existe nao aparece
      (camada de OCR antigo, invisivel ou escondida embaixo da imagem).
    Pagina com timbrado de pagina inteira e texto visivel por cima fica nativa.
    """
    if chars_pdf < MIN_CHARS:
        return True
    if cobertura_imagem >= MIN_COBERTURA and texto_visivel < MIN_VISIVEL:
        return True
    return False


def vale_perguntar_giro(confianca):
    """OCR com confianca baixa pode ser pagina de lado: vale rodar o OSD."""
    return confianca < MIN_CONFIANCA_SEM_GIRO


def fica_com_girada(confianca_original, confianca_girada):
    """So aceita o giro se o OCR da pagina girada for bem melhor que o original."""
    return confianca_girada >= confianca_original + GANHO_MIN_GIRO


def imagens_da_pagina(page):
    """Cada imagem da pagina: (xref, retangulo, fracao da area da pagina).

    xref e o numero da imagem dentro do PDF. A mesma imagem pode aparecer em
    mais de um lugar (e em varias paginas, como o timbrado); aqui vale o
    maior lugar em que ela aparece nesta pagina.
    """
    area_pagina = abs(page.rect)
    lista = []
    # set(): a mesma imagem pode vir repetida na lista do PyMuPDF
    for xref in {img[0] for img in page.get_images(full=True)}:
        maior_rect, maior_fracao = None, 0.0
        for rect in page.get_image_rects(xref):
            # "& page.rect" corta o que passa da borda: as vezes a imagem e
            # maior que a pagina e a conta daria mais que 100%
            rect = rect & page.rect
            fracao = abs(rect) / area_pagina
            if fracao > maior_fracao:
                maior_rect, maior_fracao = rect, fracao
        if maior_rect is not None:
            lista.append((xref, maior_rect, maior_fracao))
    return lista


def cobertura_imagem(page):
    """Fracao da area da pagina coberta pela maior imagem que ela contem.

    Uma pagina escaneada e uma imagem so, do tamanho da pagina (cobertura ~1).
    Um PDF do Word tem no maximo um logotipo (cobertura ~0,1), nada (0) ou,
    se o timbrado for uma imagem de fundo, ~1 tambem (ver texto_visivel).
    """
    return max((fracao for _, _, fracao in imagens_da_pagina(page)), default=0.0)


def texto_visivel(page):
    """Fracao do texto nativo que aparece de verdade, por cima das imagens.

    O PDF desenha a pagina numa ordem (imagem, texto, linha...). O PyMuPDF da
    essa ordem (get_bboxlog) e, pra cada trecho de texto, o numero de ordem e
    o modo de desenho (get_texttrace). Texto que NAO aparece:
    - invisivel (modo 3, ou opacidade 0): o jeito do Acrobat guardar OCR;
    - desenhado ANTES da imagem grande: fica embaixo dela, coberto (scanner).
    Devolve 0 se a pagina nao tem texto nenhum.
    """
    area_pagina = abs(page.rect)
    ordem_imagem = None  # posicao, na ordem de desenho, da 1a imagem grande
    for i, (tipo, rect) in enumerate(page.get_bboxlog()):
        if "image" in tipo and abs(pymupdf.Rect(rect) & page.rect) / area_pagina >= MIN_COBERTURA:
            ordem_imagem = i
            break

    total = 0
    visivel = 0
    for trecho in page.get_texttrace():
        n = len(trecho["chars"])
        total += n
        invisivel = trecho["type"] == 3 or trecho["opacity"] == 0
        embaixo = ordem_imagem is not None and trecho["seqno"] < ordem_imagem
        if not invisivel and not embaixo:
            visivel += n
    if total == 0:
        return 0.0
    return visivel / total


def nome_saida(pdf, pasta_saida):
    """Caminho do .jsonl de um PDF: <saida>/<CLASSE>/<mesmo nome>.jsonl.

    A classe vem do NOME do arquivo (ADI_2945_... -> ADI), nao da pasta onde
    ele esta. Assim a amostra, que e uma pasta plana, sai organizada igual.
    """
    pdf = Path(pdf)
    classe = pdf.stem.split("_")[0]
    return Path(pasta_saida) / classe / (pdf.stem + ".jsonl")


def processo_do_nome(nome):
    """'ADI_2945_inicial_atualizada.pdf' -> 'ADI 2945' (pro log ficar legivel)."""
    partes = Path(nome).stem.split("_")
    return f"{partes[0]} {partes[1]}"


# ---------------------------------------------------------------- estado e log
# Mesmo esquema do baixar_iniciais_stf.py: o checkpoint e um JSON com o que ja
# foi feito, e o log e um CSV com uma linha por PDF. Os dois ficam dentro da
# pasta de saida, entao a amostra e o corpus nao se misturam.


def carrega_estado(pasta_saida):
    arq = pasta_saida / "_estado.json"
    if arq.exists():
        return json.loads(arq.read_text(encoding="utf-8"))
    return {"feitos": {}}


def salva_estado(pasta_saida, estado):
    pasta_saida.mkdir(parents=True, exist_ok=True)
    arq = pasta_saida / "_estado.json"
    arq.write_text(json.dumps(estado, ensure_ascii=False, indent=1),
                   encoding="utf-8")


def log_linha(pasta_saida, linha):
    arq = pasta_saida / "_log.csv"
    novo = not arq.exists()
    with arq.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["processo", "arquivo", "paginas", "nativas", "ocr",
                        "ocr_falhou", "giradas", "com_figuras", "confianca_media",
                        "segundos", "status", "detalhe"])
        w.writerow(linha)


# ---------------------------------------------------------------- processos do OCR
# Tudo daqui ate "processo principal" roda DENTRO dos processos do pool.
# Cada processo abre o seu proprio Tesseract (um Tesseract nao pode ser
# dividido entre processos) e guarda nas variaveis globais abaixo, que so
# existem dentro dele.

_tess = None  # Tesseract com o modelo de portugues
_osd = None  # Tesseract no modo "so descobrir a orientacao da pagina"
_pdf_aberto = (None, None)  # (caminho, documento): o ultimo PDF aberto
_figuras_lidas = {}  # xref -> texto: OCR de imagem que ja foi feito neste PDF


def inicia_processo(pasta_modelos):
    """Roda uma vez quando o processo do pool nasce: abre os dois Tesseracts."""
    global _tess, _osd
    # Ctrl+C: quem trata e o processo principal. Se os processos do pool
    # tambem recebessem, cada um morreria no meio de uma pagina com um
    # traceback na tela.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _tess = tesserocr.PyTessBaseAPI(path=str(pasta_modelos), lang=IDIOMA)
    _osd = tesserocr.PyTessBaseAPI(path=str(pasta_modelos), lang="osd",
                                   psm=tesserocr.PSM.OSD_ONLY)


def abre_pdf(caminho):
    """Abre o PDF, ou devolve o que ja esta aberto.

    As paginas de um PDF vem seguidas, entao cada processo abre cada PDF uma
    vez so, e nao uma vez por pagina.
    """
    global _pdf_aberto, _figuras_lidas
    if _pdf_aberto[0] != caminho:
        if _pdf_aberto[1] is not None:
            _pdf_aberto[1].close()
        _pdf_aberto = (caminho, pymupdf.open(caminho))
        _figuras_lidas = {}  # o xref so vale dentro do mesmo PDF
    return _pdf_aberto[1]


def renderiza(page, dpi, recorte=None):
    """Desenha a pagina (ou so o `recorte` dela) como imagem em tons de cinza.

    Em tons de cinza: o OCR nao precisa de cor e a imagem fica 3x menor na
    memoria (importa numa pagina A4 a 300 dpi, que tem ~8,7 milhoes de px).
    """
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, clip=recorte)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def imagem_original(doc, xref, rect, dpi):
    """A imagem como ela esta guardada dentro do PDF, sem nada por cima.

    Diferente de renderizar o pedaco da pagina: no timbrado, o texto nativo
    esta desenhado por cima da imagem, e renderizar traria esse texto junto
    (sairia duplicado em "texto_figuras").
    """
    info = doc.extract_image(xref)
    imagem = Image.open(io.BytesIO(info["image"]))
    # transparencia (mascara guardada a parte, ou canal alfa): fundo branco,
    # senao o que e transparente pode virar preto e cobrir o texto
    if info.get("smask"):
        mascara = Image.open(io.BytesIO(doc.extract_image(info["smask"])["image"]))
        imagem = imagem.convert("RGBA")
        imagem.putalpha(mascara.convert("L").resize(imagem.size))
    if imagem.mode in ("RGBA", "LA", "P", "PA"):
        imagem = imagem.convert("RGBA")
        fundo = Image.new("RGBA", imagem.size, "white")
        imagem = Image.alpha_composite(fundo, imagem)
    imagem = imagem.convert("L")

    # Resolucao: um print colado no Word costuma ter ~100 dpi, e o Tesseract
    # erra muito abaixo de 300. Calcula o dpi em que a imagem aparece na
    # pagina (pixels / polegadas; 72 pontos = 1 polegada) e reescala pra 300.
    # Aumento no maximo 4x: alem disso a imagem e ruim demais pra ajudar.
    dpi_na_pagina = imagem.width / (rect.width / 72)
    escala = min(dpi / dpi_na_pagina, 4)
    if abs(escala - 1) > 0.1:
        novo_tamanho = (round(imagem.width * escala), round(imagem.height * escala))
        imagem = imagem.resize(novo_tamanho, Image.LANCZOS)
    return imagem


def ocr(imagem):
    """Manda a imagem pro Tesseract. Devolve (texto, confianca de 0 a 100)."""
    _tess.SetImage(imagem)
    texto = _tess.GetUTF8Text()
    # o Tesseract as vezes termina o texto com um "form feed" (\x0c); nao serve pra nada
    return texto.replace("\x0c", ""), _tess.MeanTextConf()


def orientacao(imagem):
    """Pergunta ao OSD quantos graus girar a imagem pra ela ficar em pe.

    Devolve 0, 90, 180 ou 270. Se o OSD nao conseguir decidir (pagina quase
    vazia, so desenho), devolve 0.
    """
    _osd.SetImage(imagem)
    try:
        resposta = _osd.DetectOrientationScript()
    except Exception:
        return 0
    if not resposta:
        return 0
    return resposta["orient_deg"]


def ocr_com_giro(imagem):
    """OCR, e se sair ruim, tenta de novo com a pagina girada.

    Devolve (texto, confianca, giro). Ver MIN_CONFIANCA_SEM_GIRO pro porque
    das duas conferencias.
    """
    texto, confianca = ocr(imagem)
    if not vale_perguntar_giro(confianca):
        return texto, confianca, 0

    graus = orientacao(imagem)
    if graus == 0:
        return texto, confianca, 0

    # PIL gira no sentido anti-horario; o OSD responde quanto girar nesse
    # sentido (conferido girando uma pagina de proposito nos 4 sentidos)
    texto_girado, confianca_girada = ocr(imagem.rotate(graus, expand=True))
    if fica_com_girada(confianca, confianca_girada):
        return texto_girado, confianca_girada, graus
    return texto, confianca, 0


def texto_das_figuras(doc, page, dpi):
    """OCR das imagens grandes de uma pagina nativa. Devolve (texto, falhas)."""
    textos = []
    falhas = 0
    for xref, rect, fracao in imagens_da_pagina(page):
        if fracao < MIN_COBERTURA_FIGURA:
            continue
        # o timbrado e a mesma imagem em todas as paginas: le uma vez so
        if xref not in _figuras_lidas:
            try:
                try:
                    imagem = imagem_original(doc, xref, rect, dpi)
                except Exception:
                    # formato que o PIL nao abre: desenha o pedaco da pagina
                    imagem = renderiza(page, dpi, rect)
                texto, _, _ = ocr_com_giro(imagem)
                _figuras_lidas[xref] = texto.strip()
            except Exception as e:
                print(f"  [figura] {Path(doc.name).name} pagina {page.number + 1} "
                      f"falhou: {type(e).__name__}: {str(e)[:80]}", flush=True)
                falhas += 1
                continue
        if _figuras_lidas[xref]:
            textos.append(_figuras_lidas[xref])
    return "\n\n".join(textos), falhas


def extrai_pagina(tarefa):
    """Devolve o dicionario de uma pagina (a linha do JSONL).

    `tarefa` e uma tupla (caminho do pdf, numero da pagina, forcar_ocr, dpi):
    uma tupla simples porque ela viaja do processo principal pro processo do
    pool, e tudo o que viaja entre processos precisa ser "picklavel".
    """
    caminho, numero, forcar_ocr, dpi = tarefa
    doc = abre_pdf(caminho)
    page = doc[numero - 1]

    texto_pdf = page.get_text()
    chars_pdf = len(texto_pdf.strip())
    cobertura = round(cobertura_imagem(page), 2)
    visivel = round(texto_visivel(page), 2)

    linha = {
        "pagina": numero,
        "origem": "nativo",
        "chars_pdf": chars_pdf,
        "cobertura_imagem": cobertura,
        "texto_visivel": visivel,
        "confianca_ocr": None,
        "giro": 0,
        "texto": texto_pdf,
        "texto_figuras": "",
        "figuras_falharam": 0,  # so pro log; sai da linha antes de gravar
    }

    if forcar_ocr or precisa_ocr(chars_pdf, cobertura, visivel):
        try:
            texto, confianca, giro = ocr_com_giro(renderiza(page, dpi))
            linha.update(origem="ocr", texto=texto, confianca_ocr=confianca, giro=giro)
        except Exception as e:
            # a pagina fica registrada como falha, com texto vazio, e o
            # documento continua. Melhor saber qual pagina faltou do que
            # perder o documento inteiro por causa de uma.
            print(f"  [ocr] {Path(caminho).name} pagina {numero} falhou: "
                  f"{type(e).__name__}: {str(e)[:80]}", flush=True)
            linha.update(origem="ocr_falhou", texto="")
    else:
        texto, falhas = texto_das_figuras(doc, page, dpi)
        linha.update(texto_figuras=texto, figuras_falharam=falhas)

    return linha


# ---------------------------------------------------------------- processo principal


def abre_pool(processos):
    """Abre os processos do OCR. Cada um roda inicia_processo uma vez ao nascer."""
    return ProcessPoolExecutor(max_workers=processos, initializer=inicia_processo,
                               initargs=(TESSDATA,))


def extrai_pdf(pool, pdf, destino, forcar_ocr, dpi):
    """Extrai todas as paginas de um PDF e grava o .jsonl em `destino`.

    As paginas vao pro pool todas de uma vez; pool.map devolve as respostas
    na ordem das paginas, entao o .jsonl sai em ordem mesmo com as paginas
    sendo feitas em paralelo.

    Devolve a contagem: {"nativo": 10, "ocr": 2, ..., "confiancas": [...]}.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    # escreve num .tmp e so renomeia no fim: se der Ctrl+C no meio, nao
    # sobra um .jsonl pela metade parecendo que esta completo
    tmp = destino.with_suffix(".jsonl.tmp")
    contagem = {"nativo": 0, "ocr": 0, "ocr_falhou": 0, "giradas": 0,
                "com_figuras": 0, "figuras_falharam": 0, "confiancas": []}

    # abre aqui so pra saber quantas paginas tem (e pra um PDF que nao abre
    # dar erro aqui, com uma mensagem clara)
    with pymupdf.open(pdf) as doc:
        n_paginas = doc.page_count
    tarefas = [(str(pdf), i, forcar_ocr, dpi) for i in range(1, n_paginas + 1)]

    try:
        with tmp.open("w", encoding="utf-8") as f:
            for linha in pool.map(extrai_pagina, tarefas):
                contagem[linha["origem"]] += 1
                contagem["figuras_falharam"] += linha.pop("figuras_falharam")
                if linha["giro"]:
                    contagem["giradas"] += 1
                if linha["texto_figuras"]:
                    contagem["com_figuras"] += 1
                if linha["confianca_ocr"] is not None:
                    contagem["confiancas"].append(linha["confianca_ocr"])
                # ensure_ascii=False: grava "ção" como "ção", nao como "ção"
                f.write(json.dumps(linha, ensure_ascii=False) + "\n")
        tmp.rename(destino)
    except BaseException:
        # BaseException pra pegar tambem o KeyboardInterrupt (Ctrl+C)
        tmp.unlink(missing_ok=True)
        raise

    return contagem


# ---------------------------------------------------------------- preparacao


def sha256(arquivo):
    """Hash do arquivo, pra conferir se o modelo baixado e o esperado."""
    h = hashlib.sha256()
    with open(arquivo, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def garante_modelos():
    """Baixa os modelos que faltam (ou que vieram errados) pra modelos/tessdata_best/."""
    TESSDATA.mkdir(parents=True, exist_ok=True)
    for nome, hash_esperado in MODELOS.items():
        arquivo = TESSDATA / nome
        if arquivo.exists() and sha256(arquivo) == hash_esperado:
            continue
        print(f"Baixando o modelo {nome} do Tesseract (so na primeira vez)...")
        # baixa pra um .tmp e renomeia no fim, pelo mesmo motivo do .jsonl: se a
        # conexao cair no meio, nao fica um modelo pela metade com o nome certo
        tmp = arquivo.with_suffix(".tmp")
        urllib.request.urlretrieve(URL_MODELOS + nome, tmp)
        if sha256(tmp) != hash_esperado:
            tmp.unlink()
            raise SystemExit(f"O {nome} baixado nao bate com o hash esperado. "
                             "Tente de novo; se repetir, o arquivo mudou no GitHub.")
        tmp.rename(arquivo)


def confere_tesseract():
    """Abre o Tesseract uma vez no processo principal, so pra conferir.

    Se algo estiver errado (modelo corrompido, wheel sem o Tesseract), quebra
    aqui, no inicio, com uma mensagem clara, e nao dentro dos processos do
    pool, onde o erro seria bem mais confuso.
    """
    garante_modelos()
    for idioma in (IDIOMA, "osd"):
        tesserocr.PyTessBaseAPI(path=str(TESSDATA), lang=idioma).End()
    versao = tesserocr.tesseract_version().splitlines()[0]
    print(f"{versao}, modelos tessdata_best ({IDIOMA} + osd)")
    return versao


def lista_pdfs(pasta, so_classe, limite):
    """Todos os PDFs da pasta (e subpastas), em ordem fixa de nome."""
    pdfs = sorted(pasta.rglob("*.pdf"))
    if so_classe:
        pdfs = [p for p in pdfs if p.name.split("_")[0] == so_classe.upper()]
    if limite:
        pdfs = pdfs[:limite]
    return pdfs


# ---------------------------------------------------------------- main


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--pasta", default=str(ENTRADA), help="pasta com os PDFs")
    p.add_argument("--saida", default=str(SAIDA), help="pasta dos .jsonl")
    p.add_argument("--limite", type=int, default=0)
    p.add_argument("--so-classe", default="")
    p.add_argument("--refazer", action="store_true")
    p.add_argument("--forcar-ocr", action="append", default=[],
                   help="nome do PDF (sem .pdf) pra fazer OCR em todas as paginas")
    p.add_argument("--processos", type=int, default=PROCESSOS)
    p.add_argument("--dpi", type=int, default=DPI)
    args = p.parse_args()

    pasta = Path(args.pasta)
    pasta_saida = Path(args.saida)
    # aceita "ADI_3494_inicial" ou "ADI_3494_inicial.pdf", tanto faz
    forcar = {Path(n).stem for n in args.forcar_ocr}

    versao = confere_tesseract()
    pdfs = lista_pdfs(pasta, args.so_classe, args.limite)
    estado = carrega_estado(pasta_saida)
    # guarda com que configuracao a carga foi feita, pra saber depois
    estado["config"] = {"tesseract": versao, "modelos": f"tessdata_best@{COMMIT_MODELOS[:7]}",
                        "dpi": args.dpi}
    print(f"{len(pdfs)} PDFs em {pasta} -> {pasta_saida} | {args.dpi} dpi | "
          f"{args.processos} processos")

    pool = abre_pool(args.processos)
    resumo = {}
    inicio_carga = time.time()
    try:
        for n, pdf in enumerate(pdfs, 1):
            proc = processo_do_nome(pdf.name)
            destino = nome_saida(pdf, pasta_saida)

            if estado["feitos"].get(pdf.name) == "ok" and not args.refazer:
                resumo["pulado"] = resumo.get("pulado", 0) + 1
                continue

            inicio = time.time()
            try:
                c = extrai_pdf(pool, pdf, destino, pdf.stem in forcar, args.dpi)
            except KeyboardInterrupt:
                raise
            except BrokenProcessPool:
                # Um processo do pool morreu de vez (falha dentro do Tesseract,
                # falta de memoria). O pool inteiro fica inutilizavel, entao
                # registra o PDF como erro, abre um pool novo e segue. Rodar de
                # novo depois tenta esse PDF outra vez.
                segundos = round(time.time() - inicio)
                log_linha(pasta_saida, [proc, pdf.name, 0, 0, 0, 0, 0, 0, "", segundos,
                                        "erro", "um processo do OCR morreu"])
                estado["feitos"][pdf.name] = "erro"
                salva_estado(pasta_saida, estado)
                resumo["erro"] = resumo.get("erro", 0) + 1
                print(f"[{n}/{len(pdfs)}] {proc:<10} erro   processo do OCR morreu", flush=True)
                pool.shutdown(wait=False, cancel_futures=True)
                pool = abre_pool(args.processos)
                continue
            except Exception as e:
                # um PDF com problema nao derruba a carga inteira
                segundos = round(time.time() - inicio)
                log_linha(pasta_saida, [proc, pdf.name, 0, 0, 0, 0, 0, 0, "", segundos,
                                        "erro", f"{type(e).__name__}: {str(e)[:120]}"])
                estado["feitos"][pdf.name] = "erro"
                salva_estado(pasta_saida, estado)
                resumo["erro"] = resumo.get("erro", 0) + 1
                print(f"[{n}/{len(pdfs)}] {proc:<10} erro   {type(e).__name__}", flush=True)
                continue

            segundos = round(time.time() - inicio)
            paginas = c["nativo"] + c["ocr"] + c["ocr_falhou"]
            confs = c["confiancas"]
            confianca_media = round(sum(confs) / len(confs)) if confs else ""
            detalhes = []
            if c["ocr_falhou"]:
                detalhes.append(f"{c['ocr_falhou']} pagina(s) com ocr_falhou")
            if c["figuras_falharam"]:
                detalhes.append(f"{c['figuras_falharam']} figura(s) sem OCR")
            log_linha(pasta_saida, [proc, pdf.name, paginas, c["nativo"], c["ocr"],
                                    c["ocr_falhou"], c["giradas"], c["com_figuras"],
                                    confianca_media, segundos, "ok", "; ".join(detalhes)])
            estado["feitos"][pdf.name] = "ok"
            salva_estado(pasta_saida, estado)
            resumo["ok"] = resumo.get("ok", 0) + 1
            print(f"[{n}/{len(pdfs)}] {proc:<10} ok     {paginas:>5} pag "
                  f"({c['ocr']} ocr, {c['com_figuras']} c/ figura) {segundos:>5} s", flush=True)
    except KeyboardInterrupt:
        print("\nInterrompido. Rode de novo pra retomar de onde parou.")
    finally:
        salva_estado(pasta_saida, estado)
        # cancel_futures: descarta as paginas que ainda nem comecaram
        pool.shutdown(wait=True, cancel_futures=True)

    minutos = round((time.time() - inicio_carga) / 60, 1)
    print(f"\nResumo: {resumo} em {minutos} min")
    print(f"Textos em: {pasta_saida.resolve()}")
    print(f"Log em   : {(pasta_saida / '_log.csv').resolve()}")


if __name__ == "__main__":
    main()
