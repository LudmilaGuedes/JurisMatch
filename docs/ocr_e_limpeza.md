# Fase 2 — Extração de texto + OCR + limpeza

Etapa que transforma os PDFs das petições iniciais do STF em texto consultável,
sem perder informação relevante.

- **Objetivo da extração:** ler o texto de todas as páginas, inclusive as que são
  imagem escaneada (sem camada textual).
- **Objetivo da limpeza:** tirar o que não é conteúdo — carimbo de cabeçalho/rodapé,
  bloco de assinatura digital, numeração de página — preservando integralmente a
  argumentação da petição.

---

## Estrutura

A organização segue a sugestão do professor: cada petição tem um **ID estável**
(`<CLASSE>_<NÚMERO>`, ex.: `ADI_6002`), e tudo o que é produzido a partir dela leva o
mesmo ID. Assim se mantém a relação **documento → página → chunk → resultado da busca**.

```
Projeto_final/
├─ peticoes_iniciais/            PDFs baixados do STF (não é alterada)
├─ peticoes_teste/               PDFs de teste (não é alterada) ← origem da base atual
├─ data/
│  ├─ 01_raw/pdf/<ID>.pdf        cópia do PDF com o ID como nome (nunca alterada)
│  └─ 02_processed/
│     ├─ ocr/<ID>.json           texto de cada página como saiu da extração
│     └─ clean/<ID>.json         texto limpo de cada página ← é este que se usa
├─ metadata/documentos.csv       uma linha por documento: processo, relator,
│                                assuntos... + status e qualidade do processamento
├─ logs/processing.csv           uma linha por documento e etapa: status, tempo, erro
├─ scripts/
│  ├─ 00_registrar_documentos.py dá o ID, copia o PDF, cria o documentos.csv
│  └─ converter_textos_antigos.py  conversão única do formato antigo (textos/*.md)
├─ src/ocr_pipeline.py           extração + OCR + limpeza
├─ ocr_padrao.ipynb              notebook que executa e mostra os resultados
└─ textos/                       saída da versão 1 (Markdown); substituída por data/
```

**Fluxo:**

```
peticoes_teste/*.pdf
   │  scripts/00_registrar_documentos.py
   ▼
data/01_raw/pdf/<ID>.pdf  +  metadata/documentos.csv
   │  src/ocr_pipeline.py
   ▼
data/02_processed/ocr/<ID>.json  →  limpeza  →  data/02_processed/clean/<ID>.json
```

**ID e versões.** O ID vem da classe e do número do processo, lidos do nome do arquivo.
Quando há mais de um PDF para o mesmo processo, só um entra na base, e a escolha fica
registrada no script (`VERSAO_PREFERIDA`). A `ADI_2945` tinha duas versões: entrou a
`inicial_atualizada`, que tem todas as páginas da outra e mais 3 termos processuais. A
descartada aparece na coluna `versoes_descartadas`. Um processo com várias versões e sem
escolha registrada faz o script parar, em vez de escolher em silêncio.

**PDF original intocado.** O registro **copia** o PDF para `data/01_raw/pdf/` e confere a
cópia pelo SHA-256. A pasta de origem não é renomeada nem alterada.

**Por que duas camadas (`ocr/` e `clean/`).** A limpeza é destrutiva e o OCR é caro.
Guardando o texto de antes da limpeza, se alguma regra remover algo relevante, dá para
conferir o que foi perdido e reajustar a regra sem reprocessar os PDFs.

**Por que JSON por página.** Cada arquivo tem uma entrada por página do PDF, inclusive as
que ficam vazias depois da limpeza. Assim um trecho sempre pode ser citado com a página de
origem ("veio da página 17"), o que a etapa de chunks vai usar para registrar
`page_start`/`page_end`:

```json
{
  "id": "ADI_5823",
  "arquivo_pdf": "ADI_5823.pdf",
  "sha256": "b8c1bcaa…",
  "etapa": "clean",
  "versao_pipeline": "2.1",
  "processado_em": "2026-10-06T10:20:08",
  "resumo": {"paginas": 28, "paginas_digital": 28, "paginas_ocr": 0, "metodo": "digital",
             "chars_ocr": 83057, "chars_clean": 76343, "reducao_pct": 8.1, "...": "..."},
  "pages": [
    {"page": 1, "metodo": "digital", "conf": null, "dpi": null, "rotacao": 0,
     "text": "GORDILHO, PAVIE E AGUIAR ADVOGADOS …"},
    {"page": 2, "metodo": "ocr", "conf": 88.4, "dpi": 300, "rotacao": 0, "text": "…"}
  ]
}
```

---

## Passo a passo do pipeline

### Passo 1 — Decidir, **página a página**, entre texto digital e OCR

Para cada página: `page.get_text()` (PyMuPDF). Se sobrarem pelo menos
**100 caracteres úteis** (`MIN_CHARS_PAGINA`), a página tem camada textual e o texto
digital é usado direto. Abaixo disso, a página é imagem e vai para o OCR.

> **Por quê página a página, e não por documento.** Existem petições **mistas**: o
> processo é nativo digital, mas algumas páginas foram anexadas como imagem escaneada.
> Uma verificação feita no documento inteiro (`if texto.strip():`) classifica esse
> arquivo como "digital" e **essas páginas saem vazias, em silêncio** — perda de
> informação sem nenhum aviso. Na amostra de teste isso acontece de verdade: o
> `ADI_3494_inicial` tem camada textual em 200 das suas 201 páginas — a 89 é imagem, e
> passaria batida numa verificação por documento.

O texto digital, quando existe **e é bom**, é sempre preferido ao OCR: é o texto original,
sem erro de reconhecimento.

**Checagem de qualidade da camada textual.** Ter texto não basta. Alguns PDFs trazem uma
camada textual que é lixo: OCR antigo de baixa qualidade gravado no próprio PDF, ou fonte
embutida sem tabela de caracteres (o texto sai como `�` ou símbolos de uso privado). A
checagem só de tamanho aceitaria essas páginas. Por isso cada página com texto passa por
`avaliar_camada_textual`, que a reprova se:

- mais de 2% dos caracteres são inválidos (`�`, uso privado, controle), ou
- mais de 5% das palavras são suspeitas: letras e dígitos alternando mais de uma vez
  (`c0nst1tu1cao`), dois ou mais símbolos estranhos (`(~C,:t-·`), letra repetida 4+ vezes,
  ou 6+ letras sem vogal.

A página reprovada vai para o OCR e fica marcada como `ocr_fallback`. A regra foi
calibrada nas 2.386 páginas com texto da base: reprova só 7, todas no `ADI_3494` e todas
lixo de verdade (`Mini!l1l.éIio`, `INCONSTITl'C10NAL`). Percentuais (`27,5%`), numeração de
seção (`II.1.2`), `CF/88`, `art. 28-A` e URLs não contam como suspeitos. Uma primeira versão
mais ingênua reprovava 18 páginas, e todas eram texto bom.

**Figuras embutidas.** Página digital pode ter imagem com texto: gráfico, print de tabela,
de lei ou de decisão. Esse texto não está na camada textual. Com `OCR_FIGURAS = True`, as
imagens de pelo menos 400×300 px são lidas por OCR, e o texto vai para o fim da página
com o rótulo `[figura embutida, texto via OCR]:`. Ficam de fora:

- imagem que cobre mais de 60% da página, que é o próprio escaneamento (relê-la duplicaria
  a página);
- figura com menos de 10 palavras, como logotipo ou brasão;
- imagem repetida, como o timbre presente em toda página, que é lida uma vez só por
  documento.

### Passo 2 — OCR das páginas que são imagem

1. A página é renderizada como imagem a **300 DPI** com o próprio PyMuPDF
   (`page.get_pixmap`).
2. A imagem vai para o **Tesseract** (`pytesseract`), idioma `por`, `--oem 1 --psm 3`
   (motor LSTM, detecção automática de layout).
3. O texto é reconstruído a partir de `image_to_data`, agrupando as palavras por
   bloco → parágrafo → linha.

> **Por que PyMuPDF em vez de `pdf2image` + Poppler.** O notebook original usava
> `pdf2image`, que exige instalar o Poppler à parte — no Windows isso significa baixar
> um binário e mexer no PATH. O PyMuPDF já era dependência do projeto e rasteriza o PDF
> sozinho, então some uma dependência de sistema e o mesmo código roda no Windows e no
> Colab.

> **Por que 300 DPI.** É o mínimo recomendado pelo próprio Tesseract para texto impresso.
> Abaixo disso a taxa de erro sobe bastante; acima, o ganho é pequeno e o tempo e a
> memória crescem rápido.

> **Por que `image_to_data` e não `image_to_string`.** O `image_to_data` devolve a
> confiança do reconhecimento palavra a palavra, além do texto. É isso que permite medir
> a qualidade da leitura (passo 3). Rodar as duas funções faria o OCR duas vezes na mesma
> página, o dobro do tempo.

### Passo 3 — Controle de qualidade: segunda e terceira tentativas

A confiança média das palavras da página é calculada. Se ficar abaixo de **70**
(`CONF_MINIMA`), o pipeline tenta de novo:

1. **400 DPI em escala de cinza.** Resolve leitura ruim por resolução ou contraste.
2. **Correção de rotação.** Se ainda ficar baixa, o módulo OSD do Tesseract verifica se a
   página está girada (de cabeça para baixo ou de lado, comum em anexos escaneados). Se
   estiver, a página é lida de novo na orientação certa. O OSD informa o ângulo, mas o
   sentido nem sempre bate (num caso real ele sugeriu 90° e o certo era 270°), então os dois
   sentidos são testados.

Fica sempre o resultado de maior confiança. A página girada é marcada no JSON
(`{"page": 1155, "metodo": "ocr", "conf": 87.9, "dpi": 300, "rotacao": 270, ...}`).

> **Por que a rotação importa.** Sem ela, uma página invertida não dá erro nenhum: o
> Tesseract devolve algo como `VIYNIDUVYW OHNIHAOS JIVLIA`, e o conteúdo se perde em
> silêncio. No `ADPF_165`, 11 páginas estavam nessa situação e passaram de confiança ~37
> para ~88 com a correção (por exemplo, a página de rosto de uma coletânea de legislação
> e um artigo de jornal anexado).

A confiança de cada página fica registrada no JSON (`conf`), e o `documentos.csv` traz a
média do documento. A média sozinha engana: mil páginas boas diluem as poucas ilegíveis.
Para achar as páginas que merecem conferência, filtre no JSON as páginas com `conf`
abaixo de 70 (ignorando as páginas praticamente vazias, como capas e separadores).

**Reaproveitamento do OCR.** Ao reprocessar um documento sem `--refazer`, as páginas que
já foram lidas por OCR **do mesmo PDF** (o SHA-256 guardado no `ocr/<ID>.json` confere) são
reaproveitadas, sem rodar o Tesseract de novo. As páginas digitais são sempre refeitas:
custam milissegundos e assim recebem as regras novas (checagem de qualidade, figuras).
Foi assim que a base saiu da versão 1 para a atual em 1min36s, em vez de ~1h40: das 1.561
páginas de OCR, só as 7 com camada textual reprovada do `ADI_3494` foram lidas de novo.

Cada página é processada e descartada da memória antes da próxima (`gc.collect()`), para
que documentos grandes (a amostra tem um de 1.285 páginas) não estourem a RAM.

**Salvamento parcial.** A cada 25 páginas de OCR (`SALVAR_A_CADA`) o progresso do documento
é gravado em `data/02_processed/ocr/.parcial/<ID>.json`. Se a rodada cair, a próxima retoma
dali em vez de recomeçar da página 1; quando o documento termina, o parcial é apagado.
O parcial guarda o hash SHA-256 do PDF. Se o arquivo for trocado entre uma rodada e
outra, o parcial é descartado, e páginas de um PDF antigo nunca se misturam às do novo.
Isso veio de um caso real: o OCR do `ADPF_165` (1.285 páginas, ~1h30) foi encerrado por
falta de memória na página 123 e, sem esse mecanismo, tudo o que já tinha sido lido se
perdeu. O `documentos.csv` e o log também são gravados a cada documento concluído, e não
só no fim da rodada, para que uma queda não deixe como pendente o que já terminou.

### Passo 4 — Limpeza

Roda sobre o texto extraído e gera `data/02_processed/clean/<ID>.json`, com **uma entrada
por página**, igual ao JSON de OCR. A detecção de carimbo e o vocabulário da hifenização
olham o documento inteiro, mas o texto resultante continua separado por página. Quatro
grupos de regras:

**a) Cabeçalho e rodapé repetidos — detectados, não fixos.**
Em vez de uma lista fixa de frases, a repetição é medida **dentro do próprio documento**:
as 5 primeiras e as 5 últimas linhas de cada página são normalizadas (minúsculas, sem
acento, dígitos trocados por `#`, para que "Página 3 de 40" e "Página 7 de 40" contem
como a mesma linha) e contadas.

> **Por quê assim.** Cada petição vem de um escritório ou órgão diferente e tem um
> carimbo diferente (timbre, endereço, telefone, número do documento, data e hora). Uma
> lista fixa de regras só pegaria os casos que alguém lembrou de escrever. A regra por
> repetição se adapta a cada documento.

**Densidade no trecho, não no documento inteiro.** Uma linha é carimbo quando aparece na
borda de pelo menos **5 páginas** e em pelo menos **60% das páginas entre a primeira e a
última ocorrência**. A primeira versão exigia 60% do documento inteiro e falhou num caso
real: o PDF da "petição inicial" traz a petição propriamente dita seguida de anexos
(leis, documentos). No `ADI_2945` o timbre da Procuradoria estava em praticamente todas
as páginas da petição, mas essas eram só 80 das 275 páginas (29% do documento), abaixo do
limite. Medindo no trecho, o timbre tem densidade próxima de 100%, enquanto uma linha
legítima que se repete por acaso aparece espalhada e tem densidade baixa.

**A primeira ocorrência fica, só as repetições saem.** Nos anexos, a regra também detecta
cabeçalhos corridos, como `Lei nº 13.757` no alto de cada página da lei anexada ou o nome
do deputado autor do projeto. É repetição, mas a primeira ocorrência costuma ser o
título, e a identificação da lei atacada é exatamente o tipo de informação que não pode
sumir. Mantendo a primeira ocorrência, **a limpeza nunca apaga um texto que não exista em
outro ponto do documento**. O custo é aceitável: no pior caso sobra uma cópia do timbre
na primeira página.

**Comparação tolerante ao OCR.** A comparação é feita pela linha inteira e também pelas
**pontas da linha** (os 15 primeiros ou os 15 últimos caracteres). No OCR a mesma linha de
timbre sai diferente a cada página, e num timbre escaneado o erro costuma cair numa das
pontas (o logotipo vira `SE. |` numa página, `MV |` na outra, `Ye` na seguinte) enquanto
a outra fica estável. Guardando as duas pontas, basta uma delas se repetir. A chave curta
é deliberada: com 25 caracteres as variantes do OCR ainda escapavam.

**Tudo isso só vale na borda da página.** Nenhuma regra de carimbo remove texto do meio
da página.

A regra foi auditada linha a linha nas petições da amostra. O que ela remove é timbre
(nome do escritório ou órgão, advogados, endereço, telefone, e-mail) e cabeçalho corrido
de anexo (`Diário Oficial`, `ANTEPROJETO DE LEI — (CONTINUAÇÃO)`). Nas petições sem
timbre ela não remove nada.

**b) Bloco de assinatura digital e validação.** Regras explícitas para o que é
padronizado pelo STF/PJe: "Documento assinado digitalmente conforme MP nº 2.200-2/2001",
"Assinado eletronicamente por", "a autenticidade deste documento pode ser conferida",
"código verificador", "chave de acesso", "Impresso por", "Inteiro teor do acórdão", e a
linha contendo só "Supremo Tribunal Federal".

**c) Numeração de página solta** e **URL de autenticação** (`stf.jus.br`, `pje.jus.br`) —
as duas só são removidas quando estão sozinhas na linha **e** na borda da página. A
exigência de borda não é decorativa: o layout do PDF quebra `(doc. 21 a 23)` em linhas
soltas, e sem ela o `21` seria removido como se fosse paginação, mutilando a referência.
Uma URL no meio do texto, do mesmo modo, costuma ser citação legítima.

**d) Normalização do texto.**
- **Hífen no fim da linha:** o layout quebra `constitucional` em `consti-` / `tucional`,
  e isso precisa virar uma palavra só. Mas em português muito hífen no fim da linha é
  legítimo: `trata-` / `se`, `poluidor-` / `pagador`. A regra:
  - antes de pronome (`se`, `lhe`, `o`…) o hífen **fica** (`trata-se`, `aplica-lhe`);
  - as partes só são **juntadas** se a palavra resultante aparece em outro ponto do próprio
    documento, o que prova que é uma palavra e não duas (`constitucional`);
  - **na dúvida, o hífen fica** e só a quebra de linha sai. `consti-tucional` atrapalha uma
    busca, mas não perde informação; `poluidorpagador` perde.

  A primeira versão juntava todo hífen de fim de linha e produzia `tratase` e
  `poluidorpagador`. Na amostra, 389 hífens legítimos são preservados pela regra atual.
- **Reagrupamento de parágrafos:** o PDF quebra a linha a cada linha *visual*, então um
  parágrafo vira 8 linhas soltas e qualquer análise por sentença sai errada. Duas linhas
  só são unidas quando a primeira **não** termina em pontuação final (`.!?:;`) e a segunda
  **não** começa um item de lista ou dispositivo legal (`Art.`, `§`, `I -`, `1.`, `a)`,
  marcador). Isso preserva a estrutura de incisos e alíneas, que importa em petição
  constitucional. Também não se junta uma linha toda em maiúsculas (título de seção,
  endereçamento "EXCELENTÍSSIMO SENHOR MINISTRO…") com uma linha normal, porque o título
  não termina em pontuação e seria colado no parágrafo seguinte. Duas linhas de título
  seguidas continuam sendo unidas.
- Unicode normalizado em **NFC**, ligaduras tipográficas (`ﬁ` → `fi`), aspas curvas viram
  retas, espaços e linhas em branco excedentes colapsados. NFC e não NFKC de propósito: o
  NFKC transforma o indicador ordinal em letra, e `nº` vira `no`, `2º` vira `2o`,
  `§ 1º` vira `§ 1o`. Isso perde informação jurídica (e `no` ainda se confunde com a
  palavra comum).

**O que a limpeza deliberadamente NÃO faz:** não remove stopword, não passa tudo para
minúscula, não tira acento nem pontuação, não faz stemming. Essas transformações
dependem do tipo de análise da fase seguinte e destroem informação — o lugar delas é na
análise, não no arquivo de consulta.

**Auditoria.** Cada JSON limpo registra no `resumo` quantas linhas foram removidas e por
qual regra (`"limpeza_detalhe": {"cabecalho_rodape": 220, "carimbo": 2, ...}`); o
`documentos.csv` traz só o total (`linhas_removidas`). Dá para checar se alguma regra está
agressiva demais e ajustar o parâmetro sem refazer o OCR.

### Passo 5 — Saídas

**`data/02_processed/ocr/<ID>.json`** — texto de cada página exatamente como saiu da
extração, com método, confiança, DPI e rotação de cada página.

**`data/02_processed/clean/<ID>.json`** — mesma estrutura, texto limpo. É a camada de
consulta e a entrada da etapa de chunks.

**`metadata/documentos.csv`** — uma linha por documento. As colunas de identificação vêm do
`scripts/00_registrar_documentos.py`; as de processamento, do pipeline. Rodar o registro de
novo não apaga as colunas do pipeline.

| coluna | quem preenche | o que é |
|---|---|---|
| `id`, `arquivo_pdf`, `classe`, `numero` | registro | identificação |
| `processo`, `relator`, `numero_unico`, `data_autuacao`, `data_ultima_decisao`, `ramo_direito`, `assuntos`, `legislacao`, `meio_processo`, `situacao_decisao_final`, `link_processo` | registro | da `base_inicial_...xlsx` |
| `seq_objeto` | registro | incidente, da `base_controle_concentrado_objeto_incidente_...xlsx` |
| `paginas`, `tamanho_bytes`, `sha256` | registro | do PDF |
| `arquivo_origem`, `versoes_descartadas` | registro | de onde veio a cópia; outras versões do mesmo processo que ficaram de fora |
| `status_ocr` | pipeline | `PENDENTE` (ainda não processado), `OK`, `PENDENTE_OCR` (Tesseract indisponível) ou `ERRO` |
| `metodo` | pipeline | `digital`, `ocr` ou `misto` |
| `paginas_digital`, `paginas_ocr` | pipeline | quantas páginas de cada tipo |
| `paginas_ocr_fallback` | pipeline | das páginas de OCR, quantas tinham camada textual reprovada pela checagem de qualidade |
| `conf_ocr_media` | pipeline | confiança média do OCR (0–100), vazio se não houve OCR |
| `chars_ocr`, `chars_clean` | pipeline | tamanho do texto antes e depois da limpeza |
| `reducao_pct` | pipeline | porcentagem do texto que a limpeza tirou. Baixa não é problema (havia pouco ruído); muito alta (~30%) pede conferência |
| `linhas_removidas` | pipeline | quantas linhas a limpeza tirou (o detalhe por regra fica no `resumo` do JSON limpo) |
| `erro`, `processado_em`, `versao_pipeline` | pipeline | motivo da falha, quando houve; data e versão do processamento |

**`logs/processing.csv`** — uma linha por documento **e etapa** (`ocr`, `clean`,
`reler_baixa`): `data_hora`, `documento`, `etapa`, `status`, `segundos`, `erro`,
`versao_pipeline`. Só recebe linhas novas, nunca apaga as anteriores: é o histórico de tudo o
que foi rodado. Para achar os documentos com problema numa base grande, basta filtrar
`status != OK`.

O pipeline é **idempotente**: documentos que já têm `clean/<ID>.json` são pulados, a menos
que se peça `--refazer`. Isso permite processar a base em partes e retomar de onde parou.

**Processamento paralelo.** Com `--workers N`, N PDFs são processados ao mesmo tempo, um
por processo. O OCR é limitado por CPU, então numa máquina de 4 núcleos `--workers 3` faz a
base andar perto de 3× mais rápido. Os maiores PDFs entram primeiro, para que um escaneado
de mil páginas não fique rodando sozinho no fim. Cada Tesseract roda com uma thread só
(`OMP_THREAD_LIMIT=1`), porque várias threads por processo disputariam a CPU entre si. Nesse
modo o log mostra uma linha por documento em vez do progresso página a página, e um PDF
com erro vira `status_ocr=ERRO` no `documentos.csv` sem interromper os outros. Cada worker carrega um
PDF inteiro, então em máquina com pouca RAM é melhor usar poucos workers quando a pasta tem
escaneados muito grandes.

O `--so-digital` desliga o OCR na rodada: as petições com camada textual saem em segundos
e as escaneadas ficam com `PENDENTE_OCR`, para uma rodada dedicada depois. Serve quando a
pasta mistura petições digitais com escaneados de centenas de páginas — não faz sentido
esperar horas de OCR para ver o resultado das que são rápidas.

Para ajustar as regras de limpeza depois, existe o `--relimpar`: ele lê as páginas de
`data/02_processed/ocr/` e regrava só `clean/`. O OCR não é refeito — é a parte cara, e o
texto de cada página já está gravado.

---

## Como rodar

```bash
# 1. registrar os PDFs: dá o ID, copia para data/01_raw/pdf/ e cria o documentos.csv
python scripts/00_registrar_documentos.py                       # lê peticoes_teste/
python scripts/00_registrar_documentos.py --entrada <outra pasta>

# 2. extração + OCR + limpeza de tudo o que está em data/01_raw/pdf/
python src/ocr_pipeline.py

# idem, 3 PDFs em paralelo
python src/ocr_pipeline.py --workers 3

# reprocessar tudo do zero (refaz o OCR, sem reaproveitar páginas)
python src/ocr_pipeline.py --refazer

# reler, corrigindo rotação, só as páginas de confiança baixa já extraídas
python src/ocr_pipeline.py --reler-baixa

# refazer SÓ a limpeza, a partir de data/02_processed/ocr/, sem repetir o OCR
python src/ocr_pipeline.py --relimpar

# só o que tem camada textual; deixa os escaneados para uma rodada dedicada
python src/ocr_pipeline.py --so-digital

# Tesseract instalado fora do caminho padrão
python src/ocr_pipeline.py --tesseract "D:\Tesseract-OCR\tesseract.exe"
```

Ou pelo notebook `ocr_padrao.ipynb`, que roda o mesmo pipeline e mostra o `documentos.csv`,
uma comparação OCR × limpo de uma página e o log.

**Conversão do formato antigo.** A versão 1 do pipeline gravava Markdown em `textos/`. O
`scripts/converter_textos_antigos.py` converte esses arquivos para `data/02_processed/ocr/`
(conversão única, já feita), para o OCR já feito não ser repetido. Antes de converter, o
OCR guardado foi conferido por amostragem contra uma leitura nova das mesmas páginas: texto
idêntico em todas, e número de páginas igual ao do PDF em todos os documentos. Na
comparação palavra a palavra entre a limpeza antiga e a nova, nenhuma palavra se perdeu.
A `ADI_7919` ganhou 61, que são o texto de uma figura embutida.

### Dependências

```bash
pip install PyMuPDF pytesseract pandas pillow
```

Mais o **motor Tesseract** (programa, não biblioteca Python) com o pacote de idioma
português:

- **Windows:** `winget install --id UB-Mannheim.TesseractOCR -e` — na tela de componentes,
  marcar *Additional language data → Portuguese*.
- **Linux/Colab:** `apt-get install tesseract-ocr tesseract-ocr-por`

O pipeline procura o `tesseract.exe` sozinho nos caminhos usuais do Windows e no PATH; se
estiver em outro lugar, usar `--tesseract`. Sem o Tesseract, os arquivos que só têm texto
digital são processados normalmente e os que precisam de OCR ficam com status
`PENDENTE_OCR` — **nenhum arquivo incompleto é gravado**, então basta rodar de novo
depois de instalar.

---

## Amostra de teste

Sete petições em `peticoes_teste/`, cobrindo os três comportamentos (digital, escaneado e
misto) e uma variação grande de tamanho — de 28 a 1.285 páginas:

| arquivo | páginas | digital | OCR | caso |
|---|---|---|---|---|
| `ADI_2945_inicial` | 275 | 0 | 275 | escaneado |
| `ADI_2945_inicial_atualizada` | 278 | 3 | 275 | escaneado (mesma ADI, versão atualizada) |
| `ADI_3494_inicial` | 201 | 200 | 1 | **misto** — só a página 89 é imagem |
| `ADI_5823_inicial` | 28 | 28 | 0 | digital |
| `ADI_7919_inicial` | 221 | 221 | 0 | digital |
| `ADPF_1300_inicial` | 37 | 37 | 0 | digital |
| `ADPF_165_inicial` | 1.285 | 0 | 1.285 | escaneado, o maior |

São 2.325 páginas, das quais **1.836 precisam de OCR**. Medido com a máquina livre, o OCR
leva cerca de 3,3 s por página — algo em torno de 1h40 para a amostra inteira. Por isso o
`--so-digital`: as quatro petições com camada textual ficam prontas em segundos, sem
esperar o OCR das outras três.

O `ADI_3494` é o caso que justifica a decisão por página: 200 das 201 páginas são digitais
e passariam numa verificação por documento, mas a página 89 é imagem e sairia vazia.

---

### Resultado da amostra

| ID | método | págs (dig/ocr) | OCR por fallback | conf. OCR | chars OCR → limpo | redução |
|---|---|---|---|---|---|---|
| `ADI_2945` | misto | 3 / 275 | 0 | 85,9 | 396.696 → 390.844 | 1,5% |
| `ADI_3494` | misto | 193 / 8 | 7 | 66,6 | 593.891 → 581.066 | 2,2% |
| `ADI_5823` | digital | 28 / 0 | 0 | — | 83.057 → 76.343 | 8,1% |
| `ADI_7919` | digital | 221 / 0 | 0 | — | 552.259 → 540.535 | 2,1% |
| `ADPF_1300` | digital | 37 / 0 | 0 | — | 90.887 → 89.005 | 2,1% |
| `ADPF_165` | ocr | 0 / 1.285 | 0 | 88,0 | 2.183.808 → 2.175.050 | 0,4% |

Os seis documentos da base (a `ADI_2945` entra só na versão atualizada) saíram com
`status_ocr = OK`. Observações:

- **`ADI_3494`, confiança 66,6:** 7 das 8 páginas de OCR são `ocr_fallback`: tinham camada
  textual, mas era OCR antigo ilegível (`Mini!l1l.éIio`), e foram relidas. A página 89 é a
  única escaneada sem camada textual, um carimbo de microfilmagem com a capa de um
  estatuto, sem texto corrido. A confiança baixa dela (48,2) não representa perda de
  conteúdo.
- **`ADPF_165`:** 1.170 das 1.285 páginas saíram com confiança acima de 80. As 67 abaixo de
  70 são, em boa parte, páginas em branco, separadores e capas (57 páginas têm menos de 30
  caracteres). A redução baixa é correta: o PDF é quase todo uma coletânea de decisões de
  vários tribunais anexadas, sem um timbre único repetido. A limpeza tirou o rodapé de
  certificação do site do STJ e os cabeçalhos corridos das decisões ("Apelação Cível nº … —
  Fls. 02"), mantendo a primeira ocorrência de cada um.
- **Custo do OCR:** ~3,5 a 4,5 s por página com a máquina livre. O `ADPF_165` sozinho levou
  cerca de 1h30 de OCR. Documento digital leva segundos, qualquer que seja o tamanho.
- **Execução longa:** o OCR do `ADPF_165` foi interrompido três vezes por falta de memória
  quando rodava como tarefa em background do Claude Code. O salvamento parcial permitiu
  retomar de onde parou a cada vez. A execução que concluiu foi feita numa janela própria
  do PowerShell. Para a base completa, recomenda-se rodar assim, fora do Claude Code.

---

## Parâmetros ajustáveis

No topo de `src/ocr_pipeline.py`:

| parâmetro | padrão | efeito |
|---|---|---|
| `MIN_CHARS_PAGINA` | 100 | limite para considerar que a página tem texto |
| `MAX_PROP_TOKENS_SUSPEITOS` | 0.05 | proporção de palavras suspeitas acima da qual a camada textual é reprovada |
| `MAX_PROP_CHARS_INVALIDOS` | 0.02 | proporção de caracteres inválidos acima da qual a camada textual é reprovada |
| `OCR_FIGURAS` | True | lê por OCR as figuras embutidas em páginas digitais |
| `MIN_LARGURA_FIGURA` / `MIN_ALTURA_FIGURA` | 400 / 300 | tamanho mínimo, em pixels, da figura lida |
| `DPI_PADRAO` | 300 | resolução do OCR |
| `DPI_REFORCO` | 400 | resolução da segunda tentativa |
| `CONF_MINIMA` | 70 | confiança abaixo da qual a página é refeita |
| `SALVAR_A_CADA` | 25 | páginas de OCR entre dois salvamentos parciais |
| `FRACAO_REPETICAO` | 0.6 | densidade mínima, no trecho em que a linha aparece, para ela ser carimbo |
| `MIN_PAGINAS_BOILERPLATE` | 5 | número mínimo de páginas em que a linha precisa aparecer |
| `LINHAS_BORDA` | 5 | linhas do topo/rodapé consideradas borda |
| `CHAVE_BOILERPLATE` | 15 | caracteres das pontas na comparação tolerante a erro de OCR |
| `PADROES_ASSINATURA` | — | regras de assinatura/validação |

---

## Limitações conhecidas

- **O símbolo `§` às vezes é lido como `$` ou `8`**, e algarismos romanos de incisos podem
  sair trocados (`II` → `Il`, `I` → `1`). Isso **não é corrigido automaticamente, por
  decisão**: numa petição com valores em dinheiro, trocar `$` por `§` poderia corromper um
  valor. Quem buscar dispositivos (`§ 2º`, inciso `II`) no texto de OCR deve prever essas
  variantes na busca.
- **Tabelas escaneadas** tendem a ficar com confiança baixa (no `ADI_2945`, as tabelas de
  vencimentos anexadas respondem por boa parte das páginas com confiança abaixo de 70).

- **Tabelas e colunas** saem linearizadas. O `--psm 3` detecta a coluna, mas uma tabela
  complexa vira texto corrido.
- **Documento com menos de 5 páginas** não passa pela detecção automática de
  cabeçalho/rodapé: amostra pequena demais para a repetição significar alguma coisa.
  Nesses casos só valem as regras explícitas de assinatura.
- **Uma cópia do carimbo fica no texto** (a primeira ocorrência), de propósito: é o
  preço de nunca apagar informação que só aparece uma vez.
- **Timbre com mais de 5 linhas** escapa da detecção (fica fora da borda). Se aparecer,
  é só aumentar `LINHAS_BORDA` e rodar `--relimpar`.
- **OCR não é perfeito.** Mesmo com confiança alta há erro de reconhecimento. Por isso o
  texto digital é sempre preferido, e a confiança fica registrada por página.
- **Rodapé que muda a cada página** (frase diferente em cada uma) não é detectado pela
  regra de repetição.
