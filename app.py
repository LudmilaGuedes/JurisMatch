# -*- coding: utf-8 -*-
"""
App Streamlit da busca semantica nas peticoes iniciais (JurisMatch).

Usa a classe BuscaFAISS do src/busca_faiss.py (Projeto A: indice em data/05_faiss/,
uma entrada por peticao) e o metadata/documentos.csv para a aba do acervo.

Uso:
    python -m streamlit run app.py
"""

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))
# o torch tem que vir antes do pandas: no Windows, se o pyarrow carrega primeiro,
# a DLL do torch falha (WinError 1114). O BuscaFAISS so carrega o modelo depois.
import torch  # noqa: E402,F401

from src.busca_faiss import BuscaFAISS  # noqa: E402

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

PDFS = RAIZ / "data" / "01_raw" / "pdf"
DOCUMENTOS = RAIZ / "metadata" / "documentos.csv"

EXEMPLOS = [
    "sanções por improbidade administrativa",
    "licença-maternidade de servidoras gestantes e adotantes",
    "exigências para a carteira nacional de habilitação",
    "teto salarial e remuneração de servidores públicos",
]

st.set_page_config(page_title="JurisMatch | Pesquisa jurídica", page_icon="⚖️", layout="wide")
st.markdown(f"<style>{(RAIZ / 'styles.css').read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


@st.cache_resource(show_spinner="Carregando o modelo e o indice (so na primeira vez)...")
def carregar_indice():
    return BuscaFAISS()


@st.cache_data
def carregar_documentos():
    return pd.read_csv(DOCUMENTOS, dtype=str).fillna("")


def paginas(r):
    if r["page_start"] == r["page_end"]:
        return f"pag. {r['page_start']}"
    return f"pags. {r['page_start']}-{r['page_end']}"


def buscar(indice, consulta, k, classes, relatores):
    # ranqueia todas as peticoes no FAISS; so depois filtra e corta nos k primeiros,
    # para o filtro nao reduzir o numero de resultados
    resultados = []
    for r in indice.buscar(consulta, k=indice.n_documentos):
        if classes and indice.documentos.get(r["document_id"], {}).get("classe") not in classes:
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
        c2.metric("Similaridade", f"{r['score']:.3f}")
        st.caption(" · ".join(filter(None, [
            r.get("relator"), f"Autuação: {r.get('data_autuacao', '—')}",
            paginas(r),
        ])))
        if doc.get("ramo_direito"):
            st.markdown(f"**Ramo:** {doc['ramo_direito']}")

        st.markdown(f"> {r['trecho'][:600]}{'...' if len(r['trecho']) > 600 else ''}")
        with st.expander("Trecho completo e detalhes do processo"):
            st.write(r["trecho"])
            if doc.get("assuntos"):
                st.markdown(f"**Assuntos:** {doc['assuntos']}")
            if doc.get("legislacao"):
                st.markdown("**Legislação impugnada:**")
                st.text(doc["legislacao"].replace("_x000D_", "\n"))
            if doc.get("situacao_decisao_final"):
                st.markdown(f"**Situação:** {doc['situacao_decisao_final']}")

        b1, b2, _ = st.columns([1, 1, 3])
        if r.get("link_processo"):
            b1.link_button("Ver no portal do STF", r["link_processo"])
        pdf = PDFS / f"{r['document_id']}.pdf"
        if pdf.exists():
            b2.download_button("Baixar PDF", pdf.read_bytes(), file_name=pdf.name,
                               mime="application/pdf", key=f"pdf_{n}_{r['chunk_id']}")


def aba_busca(indice, documentos):
    trechos = indice.trechos
    with st.sidebar:
        st.markdown('<div class="brand">JurisMatch<span style="color:#91b9e4">.</span></div><div class="brand-sub">Pesquisa jurídica inteligente</div>', unsafe_allow_html=True)
        st.header("Refine sua pesquisa")
        st.caption("Combine os filtros para encontrar petições relevantes")
        k = st.slider("Quantidade de resultados", 1, 30, 5)
        classes = st.multiselect("Classe", sorted(set(documentos["classe"])))
        relatores = st.multiselect("Relator", sorted(set(documentos["relator"]) - {""}))
        st.divider()
        with st.expander("Sobre o índice de pesquisa"):
            st.caption(f"Índice: FAISS `{type(indice.indice).__name__}`")
            st.caption(f"Modelo: `{indice.info_embeddings['modelo']}`")
        st.caption(f"{len(trechos)} trechos de {indice.n_documentos} petições")

    st.subheader("O que você está pesquisando?")
    with st.form("busca"):
        consulta = st.text_area(
            "Descreva o tema ou cole um trecho de petição",
            value=st.session_state.get("consulta", ""), height=130,
            placeholder="ex.: lei estadual que invade competência privativa da União",
        )
        enviar = st.form_submit_button("Buscar petições", type="primary", use_container_width=True)

    st.caption("EXPLORE UM TEMA PARA COMEÇAR")
    cols = st.columns(len(EXEMPLOS))
    for col, ex in zip(cols, EXEMPLOS):
        if col.button(ex, use_container_width=True):
            st.session_state["consulta"] = ex
            st.rerun()

    consulta = consulta.strip()
    if not (enviar or st.session_state.get("consulta")) or not consulta:
        st.markdown('<div class="empty-state"><strong>Sua próxima referência começa aqui</strong><p>Descreva uma questão jurídica ou escolha um dos temas acima para explorar o acervo</p></div>', unsafe_allow_html=True)
        return
    st.session_state["consulta"] = consulta

    with st.spinner("Buscando..."):
        resultados = buscar(indice, consulta, k, classes, relatores)
    if not resultados:
        st.warning("Nenhum trecho com esses filtros")
        return

    docs = documentos.set_index("id").to_dict("index")
    st.subheader(f"Petições encontradas  {len(resultados)}")
    st.caption("Resultados ordenados por similaridade com sua pesquisa")
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
    st.markdown("""
    <section class="hero">
      <div class="eyebrow">Inteligência para sua pesquisa jurídica</div>
      <h1>Encontre conexões<br>Fundamente seus argumentos</h1>
      <p>Explore petições iniciais do STF com uma busca que compreende
      o sentido da sua pesquisa e aproxima você das referências relevantes</p>
      <span class="hero-tag">Controle concentrado · Ações Diretas de Inconstitucionalidade (ADI)</span>
    </section>
    """, unsafe_allow_html=True)
    indice = carregar_indice()
    documentos = carregar_documentos()
    m1, m2, m3 = st.columns(3)
    m1.metric("Petições no acervo", f"{len(documentos):,}".replace(",", "."))
    m2.metric("Trechos pesquisáveis", f"{len(indice.trechos):,}".replace(",", "."))
    m3.metric("Classes processuais", len({t["classe"] for t in indice.trechos}))
    st.write("")
    busca, acervo = st.tabs(["Pesquisa de petições", "Explorar acervo"])
    with busca:
        aba_busca(indice, documentos)
    with acervo:
        aba_acervo(documentos)
    st.markdown('<div class="footer">JurisMatch · Pesquisa semântica em petições do Supremo Tribunal Federal</div>', unsafe_allow_html=True)


main()
