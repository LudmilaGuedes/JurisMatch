# -*- coding: utf-8 -*-
"""
Etapa 00 - Registro dos documentos.

Da a cada peticao um ID estavel (<CLASSE>_<NUMERO>, ex.: ADI_6002), copia o PDF
para data/01_raw/pdf/<ID>.pdf e grava metadata/documentos.csv, cruzando com as
planilhas do STF (relator, datas, assuntos, incidente...).

O PDF de origem nunca e alterado nem movido: a pasta de entrada fica como esta,
e data/01_raw/pdf/ recebe uma copia (conferida pelo SHA-256).

Uso:
    python scripts/00_registrar_documentos.py
    python scripts/00_registrar_documentos.py --entrada peticoes_teste
    (padrao: data/01_raw/iniciais_stf/ADI, saida do baixar_iniciais_stf.py)
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
from pathlib import Path

import fitz  # PyMuPDF
import pandas as pd

RAIZ = Path(__file__).resolve().parent.parent

BASE_INICIAL = RAIZ / "data" / "00_planilhas" / "base_inicial_controle_concentrado_stf_2026_09_08.xlsx"
BASE_INCIDENTE = RAIZ / "data" / "00_planilhas" / "base_controle_concentrado_objeto_incidente_2026_09_08.xlsx"
DIR_PDF = RAIZ / "data" / "01_raw" / "pdf"
CAMINHO_DOCUMENTOS = RAIZ / "metadata" / "documentos.csv"
# Base final do projeto: as 100 ADIs da Sheet2 de casos_para_teste.xlsx, com o
# rotulo 'medida_provisoria' (1 = a ADI questiona uma medida provisoria).
CASOS_TESTE = RAIZ / "metadata" / "casos_teste.csv"

# Quando ha mais de um PDF para o mesmo processo, qual entra na base. Os demais
# ficam fora e sao listados na coluna 'versoes_descartadas'.
# ADI_2945: a versao atualizada tem todas as paginas da inicial e mais 3 termos
# processuais (autuacao e conclusao) no fim.
VERSAO_PREFERIDA = {
    "ADI_2945": "ADI_2945_inicial_atualizada.pdf",
    # ADI_7708: duas pecas "Peticao inicial" no portal (107892/2024 e 19881/2025);
    # entra a de 2024, a original que abriu o processo.
    "ADI_7708": "ADI_7708_inicial.pdf",
}

# Dois padroes de nome convivem:
#   121_ADI_6002.pdf        <linha da planilha>_<classe>_<numero>
#   ADI_2945_inicial.pdf    <classe>_<numero>_<sufixo livre>
PADRAO_NOME = re.compile(r"^(?:\d+_)?([A-Za-z]+)_(\d+)(?:_.*)?$")

# Colunas da base inicial que vao para o documentos.csv, com o nome novo.
COLUNAS_BASE_INICIAL = {
    "Processo": "processo",
    "Relator": "relator",
    "Número único": "numero_unico",
    "Data autuação": "data_autuacao",
    "Data última decisão": "data_ultima_decisao",
    "Ramo do Direito": "ramo_direito",
    "Assuntos": "assuntos",
    "Legislação": "legislacao",
    "Meio processo": "meio_processo",
    "Situação da decisão final": "situacao_decisao_final",
    "Link do processo": "link_processo",
}

COLUNAS_DOCUMENTOS = [
    "id", "arquivo_pdf", "classe", "numero", "processo", "origem",
    "relator", "numero_unico", "data_autuacao", "data_ultima_decisao",
    "ramo_direito", "assuntos", "legislacao", "meio_processo",
    "situacao_decisao_final", "seq_objeto", "link_processo", "medida_provisoria",
    "paginas", "tamanho_bytes", "sha256",
    "arquivo_origem", "versoes_descartadas", "status_ocr",
]


def sha256_arquivo(caminho):
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def id_do_nome(nome_arquivo):
    """'ADI_2945_inicial.pdf' -> 'ADI_2945'; None se o nome nao segue nenhum padrao."""
    m = PADRAO_NOME.match(Path(nome_arquivo).stem)
    if not m:
        return None
    return "%s_%s" % (m.group(1).upper(), int(m.group(2)))


def escolher_versoes(pdfs):
    """Agrupa os PDFs por ID e escolhe um por processo.

    Devolve ({id: pdf_escolhido}, {id: [descartados]}, [sem_id]). Um ID com
    varias versoes e sem entrada em VERSAO_PREFERIDA e erro: escolher em
    silencio poderia indexar a versao errada.
    """
    grupos, sem_id = {}, []
    for pdf in pdfs:
        doc_id = id_do_nome(pdf.name)
        if doc_id is None:
            sem_id.append(pdf)
        else:
            grupos.setdefault(doc_id, []).append(pdf)

    escolhidos, descartados = {}, {}
    for doc_id, versoes in sorted(grupos.items()):
        if len(versoes) == 1:
            escolhidos[doc_id] = versoes[0]
            continue
        preferida = VERSAO_PREFERIDA.get(doc_id)
        nomes = {p.name: p for p in versoes}
        if preferida not in nomes:
            raise SystemExit(
                "%s tem %d versoes (%s) e nenhuma esta em VERSAO_PREFERIDA. "
                "Decida qual entra na base e registre no script."
                % (doc_id, len(versoes), ", ".join(sorted(nomes))))
        escolhidos[doc_id] = nomes[preferida]
        descartados[doc_id] = sorted(n for n in nomes if n != preferida)
    return escolhidos, descartados, sem_id


def carregar_planilhas():
    """Base inicial + incidente (Seq. Objeto), indexadas por ID."""
    base = pd.read_excel(BASE_INICIAL, sheet_name="Sheet1")
    base["id"] = base["Classe"].str.upper() + "_" + base["Número"].astype(int).astype(str)
    base = base.rename(columns=COLUNAS_BASE_INICIAL)
    base["data_autuacao"] = pd.to_datetime(base["data_autuacao"], errors="coerce").dt.strftime("%Y-%m-%d")

    incidente = pd.read_excel(BASE_INCIDENTE, sheet_name="acervo_cc")
    incidente["id"] = (incidente["Classe"].str.upper() + "_"
                       + incidente["Número"].astype(int).astype(str))
    incidente = incidente.rename(columns={"Seq. Objeto": "seq_objeto"})

    colunas = ["id"] + list(COLUNAS_BASE_INICIAL.values())
    return (base[colunas].drop_duplicates("id").set_index("id"),
            incidente[["id", "seq_objeto"]].drop_duplicates("id").set_index("id"))


def copiar_pdf(origem, destino, hash_origem):
    """Copia o PDF e confere a copia pelo hash. Nao recopia se ja esta igual."""
    if destino.exists() and sha256_arquivo(destino) == hash_origem:
        return "ja existia"
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_suffix(".tmp")
    shutil.copy2(origem, temporario)
    if sha256_arquivo(temporario) != hash_origem:
        temporario.unlink()
        raise RuntimeError("copia de %s nao confere com o original" % origem.name)
    temporario.replace(destino)
    return "copiado"


def registrar(entrada):
    entrada = Path(entrada)
    pdfs = sorted(entrada.glob("*.pdf"))
    print("%d PDF(s) em %s\n" % (len(pdfs), entrada))

    escolhidos, descartados, sem_id = escolher_versoes(pdfs)
    for pdf in sem_id:
        print("  ! %s: nome fora dos padroes, ignorado" % pdf.name)

    base, incidente = carregar_planilhas()
    rotulos = {}
    if CASOS_TESTE.exists():
        casos = pd.read_csv(CASOS_TESTE)
        rotulos = dict(zip(casos["classe"] + "_" + casos["numero"].astype(str),
                           casos["medida_provisoria"]))

    linhas = []
    for doc_id, pdf in escolhidos.items():
        hash_pdf = sha256_arquivo(pdf)
        situacao = copiar_pdf(pdf, DIR_PDF / (doc_id + ".pdf"), hash_pdf)
        with fitz.open(pdf) as doc:
            paginas = len(doc)

        classe, numero = doc_id.split("_")
        linha = {
            "id": doc_id,
            "arquivo_pdf": doc_id + ".pdf",
            "classe": classe,
            "numero": numero,
            "origem": "STF",
            "paginas": paginas,
            "tamanho_bytes": pdf.stat().st_size,
            "sha256": hash_pdf,
            "arquivo_origem": "%s/%s" % (entrada.name, pdf.name),
            "versoes_descartadas": "; ".join(descartados.get(doc_id, [])),
            "status_ocr": "PENDENTE",
        }
        if doc_id in base.index:
            linha.update(base.loc[doc_id].to_dict())
        else:
            print("  ! %s: nao encontrado na base inicial" % doc_id)
        if doc_id in incidente.index:
            linha["seq_objeto"] = int(incidente.loc[doc_id, "seq_objeto"])
        if doc_id in rotulos:
            linha["medida_provisoria"] = int(rotulos[doc_id])

        linhas.append(linha)
        extra = " (descartada: %s)" % linha["versoes_descartadas"] if linha["versoes_descartadas"] else ""
        print("  - %-10s <- %s [%s, %d pag]%s" % (doc_id, pdf.name, situacao, paginas, extra))

    novos = (pd.DataFrame(linhas).reindex(columns=COLUNAS_DOCUMENTOS)
             .astype(object).where(lambda d: d.notna(), "").astype(str).set_index("id"))
    colunas = list(COLUNAS_DOCUMENTOS)

    # Junta com o que ja estava registrado. Os IDs registrados agora tem as
    # colunas de registro atualizadas; as colunas que o pipeline preenche
    # (status_ocr, metodo, chars...) sao mantidas, para registrar de novo nao
    # apagar o resultado do processamento.
    if CAMINHO_DOCUMENTOS.exists():
        antigos = pd.read_csv(CAMINHO_DOCUMENTOS, dtype=str,
                              keep_default_na=False).set_index("id")
        do_pipeline = [c for c in antigos.columns if c not in COLUNAS_DOCUMENTOS]
        colunas += do_pipeline
        for coluna in do_pipeline + ["status_ocr"]:
            anteriores = antigos[coluna].reindex(novos.index).fillna("")
            if coluna in novos.columns:
                novos[coluna] = anteriores.where(anteriores != "", novos[coluna])
            else:
                novos[coluna] = anteriores
        novos = pd.concat([antigos.drop(index=novos.index, errors="ignore"), novos])

    CAMINHO_DOCUMENTOS.parent.mkdir(parents=True, exist_ok=True)
    novos.reset_index().reindex(columns=colunas).fillna("").sort_values("id").to_csv(
        CAMINHO_DOCUMENTOS, index=False, encoding="utf-8-sig")
    print("\n%d documento(s) registrados em %s" % (len(linhas), CAMINHO_DOCUMENTOS))
    print("PDFs em %s" % DIR_PDF)


def main():
    parser = argparse.ArgumentParser(description="Registra as peticoes com ID estavel.")
    parser.add_argument("--entrada", default=str(RAIZ / "data" / "01_raw" / "iniciais_stf" / "ADI"),
                        help="pasta com os PDFs de origem (padrao: data/01_raw/iniciais_stf/ADI)")
    args = parser.parse_args()
    registrar(args.entrada)


if __name__ == "__main__":
    main()
