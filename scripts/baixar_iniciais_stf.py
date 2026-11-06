# -*- coding: utf-8 -*-
"""
Baixa a Peticao inicial (e a versao "Atualizada", quando existir) de cada
processo de controle concentrado do STF (ADI, ADC, ADO, ADPF).

Entrada : metadata/incidentes_stf.csv  (colunas: processo, classe, numero, incidente, ...)
Saida   : data/01_raw/iniciais_stf/<CLASSE>/<CLASSE>_<NUM>_inicial.pdf
          data/01_raw/iniciais_stf/_log.csv        -> o que deu certo e o que falhou
          data/01_raw/iniciais_stf/_estado.json    -> checkpoint, permite parar e retomar

Os caminhos sao relativos a raiz do projeto, entao da pra rodar de qualquer pasta.

Uso:
    pip install -r requirements.txt
    python scripts/baixar_iniciais_stf.py

    # o modo normal (requests) hoje leva desafio do WAF da AWS e nao passa.
    # use o navegador de verdade (abre uma janela do Chromium):
    playwright install chromium
    python scripts/baixar_iniciais_stf.py --motor playwright

Opcoes uteis:
    --limite 20         testa so nos 20 primeiros
    --min 4 --max 9     intervalo (em segundos) entre um processo e outro
    --so-classe ADC     filtra por classe
"""

import argparse
import csv
import json
import os
import random
import re
import time
from pathlib import Path

try:
    # o servidor do STF nao envia o certificado intermediario (GlobalSign
    # AlphaSSL); o truststore usa o repositorio do sistema, que completa a cadeia
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- config

BASE_PECAS = ("https://redir.stf.jus.br/estfvisualizadorpub/jsp/"
              "consultarprocessoeletronico/ConsultarProcessoEletronico.jsf"
              "?seqobjetoincidente={inc}")
BASE_PDF = ("https://redir.stf.jus.br/paginadorpub/paginador.jsp"
            "?docTP=TP&docID={doc}&prcID={inc}")
BASE_PORTAL = "https://portal.stf.jus.br/processos/detalhe.asp?incidente={inc}"

# User-Agent de um Chrome real. Um so, fixo. Nao ficamos trocando de
# identidade a cada request: isso e o que um bot evasivo faz, e o que os
# sistemas de deteccao mais facilmente pegam.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

RAIZ = Path(__file__).resolve().parent.parent
SAIDA = RAIZ / "data" / "01_raw" / "iniciais_stf"
ESTADO = SAIDA / "_estado.json"
LOG = SAIDA / "_log.csv"

RE_DOCID = re.compile(r"docID=(\d+)")

# ---------------------------------------------------------------- estado


def carrega_estado():
    if ESTADO.exists():
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    return {"feitos": {}}


def salva_estado(st):
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    ESTADO.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                      encoding="utf-8")


def log_linha(linha):
    novo = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if novo:
            w.writerow(["processo", "incidente", "status", "arquivo",
                        "bytes", "detalhe"])
        w.writerow(linha)


# ---------------------------------------------------------------- motor A
# requests: leve, rapido, e o padrao. Mantem UMA sessao viva o tempo todo,
# igual a uma aba de navegador aberta.


class MotorRequests:
    nome = "requests"

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.aquecido = False

    def aquece(self):
        """Passa pela home antes de pedir qualquer coisa, pra receber os
        cookies de sessao. Sem isso muito WAF devolve 403 no primeiro GET."""
        try:
            self.s.get("https://portal.stf.jus.br/", timeout=40)
            time.sleep(random.uniform(1.5, 3.0))
            self.aquecido = True
        except Exception as e:
            print(f"  [aviso] nao consegui aquecer a sessao: {e}")

    def get(self, url, referer=None, binario=False):
        if not self.aquecido:
            self.aquece()
        h = {}
        if referer:
            h["Referer"] = referer
        r = self.s.get(url, headers=h, timeout=120)
        return r.status_code, (r.content if binario else r.text)

    def fecha(self):
        self.s.close()


# ---------------------------------------------------------------- motor B
# playwright: navegador Chromium de verdade. Mais pesado, mas resolve
# desafio de JavaScript e tem a mesma impressao digital de TLS de um
# navegador comum, entao passa onde o requests as vezes apanha.


class MotorPlaywright:
    nome = "playwright"

    def __init__(self):
        # o STF nao envia o certificado intermediario; o Node do playwright
        # precisa dele explicitamente (o .pem fica em certs/)
        pem = RAIZ / "certs" / "stf_intermediario.pem"
        if pem.exists():
            os.environ.setdefault("NODE_EXTRA_CA_CERTS", str(pem))
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        # headless leva 403 seco do WAF; o Chromium completo, com janela,
        # recebe o desafio de JavaScript e resolve sozinho
        self.browser = self.pw.chromium.launch(headless=False, channel="chromium")
        self.ctx = self.browser.new_context(
            locale="pt-BR",
            viewport={"width": 1440, "height": 900},
        )
        self.page = self.ctx.new_page()
        self.aquecido = False

    def aquece(self):
        try:
            self.page.goto("https://portal.stf.jus.br/",
                           wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(2000)
            self.aquecido = True
        except Exception as e:
            print(f"  [aviso] nao consegui aquecer a sessao: {e}")

    def passa_desafio(self, url):
        """Abre a pagina no navegador e espera o desafio do WAF terminar,
        o que grava o cookie aws-waf-token na sessao."""
        print("  [waf] resolvendo desafio no navegador...")
        self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        try:
            self.page.wait_for_function(
                "document.documentElement.innerHTML.includes('docID=')",
                timeout=45000)
        except Exception:
            print("  [waf] desafio nao terminou a tempo")
        self.page.wait_for_timeout(1000)

    def get(self, url, referer=None, binario=False):
        if not self.aquecido:
            self.aquece()
        hd = {"Referer": referer} if referer else {}
        # ctx.request compartilha os cookies do navegador
        r = self._get_com_retry(url, hd)
        if r.status in (202, 403):
            # token do WAF ausente ou vencido: resolve na pagina de pecas
            # (para PDF, o referer e a propria pagina de pecas) e tenta de novo
            self.passa_desafio(referer if binario else url)
            r = self._get_com_retry(url, hd)
        return r.status, (r.body() if binario else r.text())

    def _get_com_retry(self, url, hd, tentativas=4):
        """Timeout e queda de conexao acontecem no servidor do STF; espera
        um pouco e tenta de novo em vez de derrubar a carga inteira."""
        for t in range(1, tentativas + 1):
            try:
                return self.ctx.request.get(url, headers=hd, timeout=120000)
            except Exception as e:
                if t == tentativas:
                    raise
                print(f"  [rede] {type(e).__name__}: {str(e)[:80]} "
                      f"(tentativa {t}/{tentativas})")
                time.sleep(30 * t)

    def fecha(self):
        try:
            self.browser.close()
            self.pw.stop()
        except Exception:
            pass


# ---------------------------------------------------------------- logica


def acha_iniciais(html):
    """Le a lista de pecas e devolve [(rotulo, docID)] das peticoes iniciais."""
    sopa = BeautifulSoup(html, "html.parser")
    vistos, achados = set(), []
    for a in sopa.find_all("a", href=True):
        m = RE_DOCID.search(a["href"])
        if not m:
            continue
        doc = m.group(1)
        if doc in vistos:
            continue
        vistos.add(doc)
        txt = " ".join(a.get_text(" ", strip=True).split())
        alvo = txt.lower()
        if alvo.startswith("peticao inicial") or alvo.startswith("petição inicial"):
            achados.append((txt, doc))
    return achados


def nome_arquivo(classe, numero, rotulo, ordem):
    atualizada = "atualizada" in rotulo.lower()
    sufixo = "inicial_atualizada" if atualizada else "inicial"
    if ordem > 0 and not atualizada:
        sufixo = f"inicial_{ordem + 1}"
    return f"{classe}_{numero}_{sufixo}.pdf"


def pausa(a, b):
    time.sleep(random.uniform(a, b))


def esfria(motivo, segundos=300):
    print(f"  [pausa longa] {motivo}. Esperando {segundos // 60} min "
          f"antes de continuar.")
    time.sleep(segundos)


def processa(motor, linha, args, estado):
    proc = linha["processo"]
    inc = linha["incidente"]
    classe = linha["classe"]
    numero = linha["numero"]

    if estado["feitos"].get(inc) == "ok" and not args.refazer:
        return "pulado"

    ref_portal = BASE_PORTAL.format(inc=inc)

    # 1) lista de pecas
    for tentativa in range(1, 5):
        code, html = motor.get(BASE_PECAS.format(inc=inc), referer=ref_portal)
        if code == 200:
            break
        if code in (403, 429, 503):
            esfria(f"{proc}: servidor devolveu {code}", 120 * tentativa)
            continue
        esfria(f"{proc}: status inesperado {code}", 60)
    else:
        log_linha([proc, inc, "erro_lista", "", 0, f"status {code}"])
        estado["feitos"][inc] = "erro"
        return "erro"

    iniciais = acha_iniciais(html)
    if not iniciais:
        log_linha([proc, inc, "sem_inicial", "", 0,
                   "nenhuma peca chamada 'Peticao inicial'"])
        estado["feitos"][inc] = "sem_inicial"
        return "sem_inicial"

    pasta = SAIDA / classe
    pasta.mkdir(parents=True, exist_ok=True)
    ok = 0

    # 2) cada PDF
    for i, (rotulo, doc) in enumerate(iniciais):
        arq = pasta / nome_arquivo(classe, numero, rotulo, i)
        if arq.exists() and arq.stat().st_size > 1000 and not args.refazer:
            ok += 1
            continue

        pausa(1.5, 3.5)
        url = BASE_PDF.format(doc=doc, inc=inc)
        ref = BASE_PECAS.format(inc=inc)

        for tentativa in range(1, 5):
            code, dados = motor.get(url, referer=ref, binario=True)
            if code == 200 and dados[:4] == b"%PDF":
                arq.write_bytes(dados)
                log_linha([proc, inc, "ok", arq.name, len(dados), rotulo])
                ok += 1
                break
            if code in (403, 429, 503):
                esfria(f"{proc}: PDF devolveu {code}", 120 * tentativa)
                continue
            log_linha([proc, inc, "erro_pdf", arq.name, 0,
                       f"status {code}"])
            break

    estado["feitos"][inc] = "ok" if ok else "erro"
    return "ok" if ok else "erro"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default=str(RAIZ / "metadata" / "incidentes_stf.csv"))
    p.add_argument("--motor", choices=["requests", "playwright"],
                   default="requests")
    p.add_argument("--min", type=float, default=4.0,
                   help="pausa minima entre processos, em segundos")
    p.add_argument("--max", type=float, default=9.0,
                   help="pausa maxima entre processos, em segundos")
    p.add_argument("--limite", type=int, default=0)
    p.add_argument("--so-classe", default="")
    p.add_argument("--refazer", action="store_true")
    args = p.parse_args()

    with open(args.csv, encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))
    if args.so_classe:
        linhas = [l for l in linhas if l["classe"] == args.so_classe.upper()]
    if args.limite:
        linhas = linhas[:args.limite]

    SAIDA.mkdir(parents=True, exist_ok=True)
    estado = carrega_estado()

    motor = MotorPlaywright() if args.motor == "playwright" else MotorRequests()
    print(f"Motor: {motor.nome} | {len(linhas)} processos | "
          f"pausa {args.min}-{args.max}s")

    contagem = {}
    try:
        for n, linha in enumerate(linhas, 1):
            try:
                r = processa(motor, linha, args, estado)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # um processo com problema nao derruba a carga inteira
                log_linha([linha["processo"], linha["incidente"], "erro_excecao",
                           "", 0, f"{type(e).__name__}: {str(e)[:120]}"])
                estado["feitos"][linha["incidente"]] = "erro"
                r = "erro"
                esfria(f"{linha['processo']}: {type(e).__name__}", 60)
            contagem[r] = contagem.get(r, 0) + 1
            print(f"[{n}/{len(linhas)}] {linha['processo']:<12} {r}")
            salva_estado(estado)
            if r != "pulado" and n < len(linhas):
                pausa(args.min, args.max)
    except KeyboardInterrupt:
        print("\nInterrompido. Rode de novo pra retomar de onde parou.")
    finally:
        salva_estado(estado)
        motor.fecha()

    print("\nResumo:", contagem)
    print(f"PDFs em: {SAIDA.resolve()}")
    print(f"Log em : {LOG.resolve()}")


if __name__ == "__main__":
    main()
