# -*- coding: utf-8 -*-
"""
Tipo de norma impugnada de cada peticao, a partir do campo `legislacao` do
metadata/documentos.csv (texto do STF, com as normas numeradas: "1 - Lei Federal
no 8429 de 1992, Art. 12 - ...  2 - Medida Provisoria no 1136 de 2022 - ...").

Usado pelo filtro "Norma impugnada" do front. Uma peticao pode impugnar mais de
uma norma, entao tipos_norma devolve um conjunto.

Lei de conversao de medida provisoria ("Conversao da Medida Provisoria no 758",
"conversao em lei da Medida Provisoria") conta como medida provisoria tambem: a
acao questiona o conteudo que veio da MP.
"""

import re

MEDIDA_PROVISORIA = "Medida provisória"
OUTROS = "Outros"

# prefixo do nome da norma no texto do STF -> categoria do filtro (a ordem importa)
CATEGORIAS = [
    ("medida provisória", MEDIDA_PROVISORIA),
    ("lei complementar", "Lei complementar"),
    ("lei federal", "Lei federal"),
    ("lei estadual", "Lei estadual"),
    ("lei distrital", "Lei estadual"),
    ("lei municipal", "Lei municipal"),
    ("emenda constitucional", "Emenda constitucional"),
    ("constituição estadual", "Constituição estadual"),
    ("constituição federal", "Constituição Federal ou ADCT"),
    ("ato das disposições constitucionais transitórias", "Constituição Federal ou ADCT"),
    ("decreto", "Decreto ou decreto-lei"),
    ("resolução", "Resolução"),
    ("portaria", "Portaria ou instrução normativa"),
    ("instrução normativa", "Portaria ou instrução normativa"),
    ("código", "Código ou CLT"),
    ("consolidação das leis do trabalho", "Código ou CLT"),
]

_CONVERSAO_MP = re.compile(r"convers\w*\s+(?:em\s+lei\s+)?da\s+medida\s+provis", re.I)


def _inicio_da_norma(n):
    """'n - ' no inicio ou depois de espaco, seguido do nome da norma (letra maiuscula)."""
    return re.compile(rf"(?:^|\s){n}\s+-\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ])")


def normas_impugnadas(legislacao):
    """Lista com o texto de cada norma do campo legislacao.

    As normas sao numeradas em sequencia ("1 - ", "2 - ", ...); procurar o proximo
    numero da sequencia evita confundir "Art. 12 - Art. 12, II" com uma norma nova.
    """
    texto = (legislacao or "").replace("_x000D_", "\n")
    m = _inicio_da_norma(1).match(texto.lstrip())
    if not m:
        return [texto.strip()] if texto.strip() else []
    texto = texto.lstrip()
    inicios, fim_do_marcador, n = [], [m.end()], 2
    while True:
        prox = _inicio_da_norma(n).search(texto, fim_do_marcador[-1])
        if not prox:
            break
        inicios.append(prox.start())
        fim_do_marcador.append(prox.end())
        n += 1
    cortes = inicios + [len(texto)]
    return [texto[a:b].strip() for a, b in zip(fim_do_marcador, cortes) if texto[a:b].strip()]


def categoria(norma):
    inicio = norma.lower()
    for prefixo, nome in CATEGORIAS:
        if inicio.startswith(prefixo):
            return nome
    return OUTROS


def tipos_norma(legislacao):
    """Conjunto de categorias das normas impugnadas (vazio se o campo estiver vazio)."""
    tipos = {categoria(n) for n in normas_impugnadas(legislacao)}
    if _CONVERSAO_MP.search(legislacao or ""):
        tipos.add(MEDIDA_PROVISORIA)
    return tipos
