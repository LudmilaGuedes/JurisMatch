# -*- coding: utf-8 -*-
"""
App Streamlit da busca semantica nas peticoes iniciais (JurisMatch).

Usa a classe Indice do scripts/buscar.py (mesmo modelo e mesmos vetores de
data/04_embeddings/) e o metadata/documentos.csv para a aba do acervo.

Uso:
    python -m streamlit run app.py
"""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "scripts"))
# o buscar (torch) tem que vir antes do pandas: no Windows, se o pyarrow carrega
# primeiro, a DLL do torch falha (WinError 1114)
from buscar import Indice, paginas  # noqa: E402

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

PDFS = RAIZ / "data" / "01_raw" / "pdf"
DOCUMENTOS = RAIZ / "metadata" / "documentos.csv"

EXEMPLOS = [
    "inconstitucionalidade de lei estadual sobre licenciamento ambiental",
    "expurgos inflacionarios da poupanca",
    "unicidade sindical",
    "vicio de iniciativa em lei que cria cargos publicos",
]

st.set_page_config(page_title="JurisMatch", page_icon="⚖️", layout="wide")


@st.cache_resource(show_spinner="Carregando o modelo e o indice (so na primeira vez)...")
def carregar_indice():
    return Indice()


@st.cache_data
def carregar_documentos():
    return pd.read_csv(DOCUMENTOS, dtype=str).fillna("")


def buscar(indice, consulta, k, um_por_peticao, classes, relatores):
    # o Indice ordena todos os trechos; filtra aqui e corta nos k primeiros
    resultados = []
    for r in indice.buscar(consulta, k=len(indice.trechos), um_por_peticao=um_por_peticao):
        if classes and r["classe"] not in classes:
            continue
        if relatores and r["relator"] not in relatores:
            continue
        resultados.append(r)
        if len(resultados) == k:
            break
    return resultados


def mostrar_resultado(n, r, docs):
    doc = docs.get(r["document_id"], {})
    with st.container(border=True):
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"#### {n}. {r['processo']}")
        c2.metric("Similaridade", f"{r['similaridade']:.3f}")
        st.caption(" · ".join(filter(None, [
            r.get("relator"), f"autuada em {r.get('data_autuacao')}",
            paginas(r), r["chunk_id"],
        ])))
        if doc.get("ramo_direito"):
            st.markdown(f"**Ramo:** {doc['ramo_direito']}")

        st.markdown(f"> {r['text'][:600]}{'...' if len(r['text']) > 600 else ''}")
        with st.expander("Trecho completo e detalhes do processo"):
            st.write(r["text"])
            if doc.get("assuntos"):
                st.markdown(f"**Assuntos:** {doc['assuntos']}")
            if doc.get("legislacao"):
                st.markdown("**Legislação impugnada:**")
                st.text(doc["legislacao"].replace("_x000D_", "\n"))
            if doc.get("situacao_decisao_final"):
                st.markdown(f"**Situação:** {doc['situacao_decisao_final']}")

        b1, b2, _ = st.columns([1, 1, 3])
        if r.get("link"):
            b1.link_button("Ver no portal do STF", r["link"])
        pdf = PDFS / f"{r['document_id']}.pdf"
        if pdf.exists():
            b2.download_button("Baixar PDF", pdf.read_bytes(), file_name=pdf.name,
                               mime="application/pdf", key=f"pdf_{n}_{r['chunk_id']}")


def aba_busca(indice, documentos):
    trechos = indice.trechos
    with st.sidebar:
        st.header("Opções da busca")
        k = st.slider("Quantidade de resultados", 1, 30, 5)
        um_por_peticao = st.checkbox("Só o melhor trecho de cada petição", value=True)
        classes = st.multiselect("Classe", sorted({t["classe"] for t in trechos}))
        relatores = st.multiselect("Relator", sorted({t["relator"] for t in trechos if t["relator"]}))
        st.divider()
        st.caption(f"Modelo: `{indice.info['modelo']}`")
        st.caption(f"{len(trechos)} trechos de {len({t['document_id'] for t in trechos})} petições")

    with st.form("busca"):
        consulta = st.text_area(
            "Descreva o tema ou cole um trecho de petição",
            value=st.session_state.get("consulta", ""), height=100,
            placeholder="ex.: lei estadual que invade competência privativa da União",
        )
        enviar = st.form_submit_button("Buscar", type="primary")

    st.caption("Exemplos:")
    cols = st.columns(len(EXEMPLOS))
    for col, ex in zip(cols, EXEMPLOS):
        if col.button(ex, use_container_width=True):
            st.session_state["consulta"] = ex
            st.rerun()

    consulta = consulta.strip()
    if not (enviar or st.session_state.get("consulta")) or not consulta:
        st.info("Digite uma consulta para encontrar as petições mais parecidas.")
        return
    st.session_state["consulta"] = consulta

    with st.spinner("Buscando..."):
        resultados = buscar(indice, consulta, k, um_por_peticao, classes, relatores)
    if not resultados:
        st.warning("Nenhum trecho com esses filtros.")
        return

    docs = documentos.set_index("id").to_dict("index")
    st.subheader(f"{len(resultados)} resultado(s)")
    for n, r in enumerate(resultados, start=1):
        mostrar_resultado(n, r, docs)


def aba_acervo(documentos):
    st.subheader(f"Petições no acervo ({len(documentos)})")
    colunas = ["processo", "relator", "data_autuacao", "ramo_direito",
               "situacao_decisao_final", "paginas", "metodo", "status_ocr", "link_processo"]
    st.dataframe(
        documentos[[c for c in colunas if c in documentos.columns]],
        hide_index=True, use_container_width=True,
        column_config={"link_processo": st.column_config.LinkColumn("link", display_text="STF")},
    )
    c1, c2 = st.columns(2)
    c1.markdown("**Por classe**")
    c1.bar_chart(documentos["classe"].value_counts())
    c2.markdown("**Por ramo do direito**")
    c2.bar_chart(documentos["ramo_direito"].replace("", "(sem ramo)").value_counts())


def main():
    st.title("⚖️ JurisMatch")
    st.caption("Busca semântica em petições iniciais de controle concentrado do STF "
               "(ADI, ADC, ADO, ADPF)")
    indice = carregar_indice()
    documentos = carregar_documentos()
    busca, acervo = st.tabs(["Busca", "Acervo"])
    with busca:
        aba_busca(indice, documentos)
    with acervo:
        aba_acervo(documentos)


main()
