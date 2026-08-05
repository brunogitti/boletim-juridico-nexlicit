"""nucleo/fatiador.py — quebra uma publicação (itens_brutos.texto_bruto) em
decisões individuais, com metadados extraídos por fonte.

Não escreve no banco, não calcula chave_dedup (isso é nucleo/dedup.py, na
Etapa 5), não chama LLM. Recebe o texto que o coletor já gravou e devolve
uma lista de DecisaoFatiada em memória.

Escopo real por fonte (reavaliado depois da Etapa 3, que mudou o desenho
de várias coletas em relação ao que docs/ARQUITETURA.md previa
originalmente — TCU virou CSV estruturada, STJ e TCE-SP/súmulas já saem
atômicos da coleta):

- TCE-PR, TCE-MG e TCE-SP/boletim: precisam fatiar de verdade (uma
  publicação com várias decisões vira vários itens).
- Zênite, STJ, TCU e TCE-SP/súmulas: já saem 1 item bruto = 1 decisão da
  coleta, mas ainda precisam ter os campos extraídos do texto livre pra
  virarem colunas estruturadas — os "extratores de metadados por fonte"
  também se aplicam a essas.
- Zênite é o único caso em que os campos ficam todos None (tribunal,
  numero_acordao etc.) — notícia em prosa não tem linha de citação
  formal, e regex de melhor esforço arriscaria inventar ou errar. Fica
  pra Etapa 5 (triagem via LLM), que já lê o texto inteiro e já tem esses
  campos no schema de saída.
"""

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass
class DecisaoFatiada:
    item_bruto_id: int
    tribunal: str | None
    numero_acordao: str | None
    numero_processo: str | None
    orgao_julgador: str | None
    relator: str | None
    data_julgamento: str | None  # ISO 8601 (AAAA-MM-DD), quando extraível
    url_inteiro_teor: str | None
    texto_decisao: str


def fatiar_item(
    nome_fonte: str,
    item_bruto_id: int,
    titulo: str,
    texto_bruto: str,
    url_origem: str,
    data_publicacao: str | None,
) -> list[DecisaoFatiada]:
    """Ponto de entrada único: despacha pro extrator certo a partir do
    nome da fonte (os mesmos nomes já semeados por nucleo.banco.seed_fontes)
    e, no caso do TCE-SP, do prefixo do título (súmula vs. edição do
    boletim)."""
    if nome_fonte.startswith("TCU"):
        return _fatiar_tcu(item_bruto_id, texto_bruto, url_origem)
    if nome_fonte.startswith("TCE-SP"):
        if titulo.startswith("Súmula"):
            return _fatiar_tce_sp_sumula(
                item_bruto_id, titulo, texto_bruto, url_origem, data_publicacao,
            )
        return _fatiar_tce_sp_boletim(item_bruto_id, texto_bruto)
    if nome_fonte.startswith("STJ"):
        return _fatiar_stj(item_bruto_id, texto_bruto, url_origem, data_publicacao)
    if nome_fonte.startswith("TCE-MG"):
        return _fatiar_tce_mg(item_bruto_id, texto_bruto)
    if nome_fonte.startswith("TCE-PR"):
        return _fatiar_tce_pr(item_bruto_id, texto_bruto)
    if nome_fonte == "Zênite":
        return _fatiar_zenite(item_bruto_id, texto_bruto)
    raise ValueError(f"fonte desconhecida pro fatiador: {nome_fonte!r}")


# --- TCE-PR ------------------------------------------------------------

_PADRAO_CITACAO_TCE_PR = re.compile(
    r'\([^,()]*?n\.º\s*([\d./]+),\s*'
    r'<a href="([^"]+)">Acórdão n\.º\s*(\d+/\d+)</a>,\s*'
    r'([^,]+),\s*'
    r'Rel\.\s*([^,]+),\s*'
    r'julgado em (\d{1,2}/\d{1,2}/\d{4})'
    r'(?:,\s*veiculado em \d{1,2}/\d{1,2}/\d{4} no DETC)?\)'
)


def _fatiar_tce_pr(item_bruto_id: int, texto_bruto: str) -> list[DecisaoFatiada]:
    decisoes = []
    inicio = 0
    for m in _PADRAO_CITACAO_TCE_PR.finditer(texto_bruto):
        numero_processo, url, numero_acordao, orgao, relator, data = m.groups()
        texto_decisao = texto_bruto[inicio:m.end()].strip()
        decisoes.append(DecisaoFatiada(
            item_bruto_id=item_bruto_id,
            tribunal="TCE-PR",
            numero_acordao=numero_acordao,
            numero_processo=numero_processo,
            orgao_julgador=_normalizar_espacos(orgao),
            relator=_normalizar_espacos(relator),
            data_julgamento=_data_br_para_iso(data),
            url_inteiro_teor=html.unescape(url),
            texto_decisao=_texto_puro(texto_decisao),
        ))
        inicio = m.end()
    return decisoes


# --- TCE-MG --------------------------------------------------------------

_MAPEAMENTO_TRIBUNAL_H2 = {
    "tribunal de contas do estado de minas gerais": "TCE-MG",
    "tribunal de contas da união": "TCU",
    "supremo tribunal federal": "STF",
    "superior tribunal de justiça": "STJ",
    "tribunal de justiça de minas gerais": "TJMG",
}

_PADRAO_TCE_MG_PROPRIO = re.compile(
    r'<p>Processo\s*<a href="([^"]+)">([^<]+)</a>\s*-\s*'
    r'[^.]+\.\s*'  # tipo de processo — sem coluna própria, descartado
    r'([^.]+)\.\s*'
    r'(?:Sess[ãa]o de|Deliberado em)\s*(\d{1,2}/\d{1,2}/\d{4})\.?'
    r'(?:\s*Publicado no DOC em \d{1,2}/\d{1,2}/\d{4}\.?)?'
    r'\s*Rel\.\s*([^<]+?)\s*</p>'
)
_PADRAO_TCE_MG_OUTRO_TRIBUNAL = re.compile(
    r"<b>Acórdão\s*(\d+/\d+)\s*([^<]+?)</b>\s*\([^,]+,\s*Relator\s*"
    r"(?:Ministro|Ministra)\s*([^)]+)\)"
)


def _fatiar_tce_mg(item_bruto_id: int, texto_bruto: str) -> list[DecisaoFatiada]:
    decisoes = []
    for tribunal, segmento in _dividir_por_h2_tce_mg(texto_bruto):
        if tribunal == "TCE-MG":
            for m in _PADRAO_TCE_MG_PROPRIO.finditer(segmento):
                url, numero_processo, orgao, data, relator = m.groups()
                decisoes.append(DecisaoFatiada(
                    item_bruto_id=item_bruto_id,
                    tribunal="TCE-MG",
                    numero_acordao=None,  # achado real: TCE-MG não cita acórdão aqui
                    numero_processo=numero_processo.strip(),
                    orgao_julgador=_normalizar_espacos(orgao),
                    relator=_normalizar_espacos(relator),
                    data_julgamento=_data_br_para_iso(data),
                    url_inteiro_teor=html.unescape(url),
                    texto_decisao=_texto_puro(m.group(0)),
                ))
        else:
            for m in _PADRAO_TCE_MG_OUTRO_TRIBUNAL.finditer(segmento):
                numero_acordao, orgao, relator = m.groups()
                decisoes.append(DecisaoFatiada(
                    item_bruto_id=item_bruto_id,
                    tribunal=tribunal,
                    numero_acordao=numero_acordao,
                    numero_processo=None,
                    orgao_julgador=_normalizar_espacos(orgao),
                    relator=_normalizar_espacos(relator),
                    data_julgamento=None,  # não informado nesse formato
                    url_inteiro_teor=None,  # achado real: sem link nesses trechos
                    texto_decisao=_texto_puro(m.group(0)),
                ))
    return decisoes


def _dividir_por_h2_tce_mg(texto_bruto: str) -> list[tuple[str | None, str]]:
    partes = re.split(r"<h2>([^<]*)</h2>", texto_bruto)
    segmentos = []
    for i in range(1, len(partes), 2):
        texto_h2 = partes[i].strip().lower()
        conteudo = partes[i + 1] if i + 1 < len(partes) else ""
        tribunal = _MAPEAMENTO_TRIBUNAL_H2.get(texto_h2)
        segmentos.append((tribunal, conteudo))
    return segmentos


# --- TCE-SP: boletim de jurisprudência (PDF) --------------------------

_PADRAO_ORGAO_TCE_SP = re.compile(
    r"^(TRIBUNAL PLENO|PRIMEIRA CÂMARA|SEGUNDA CÂMARA|TERCEIRA CÂMARA)\s*$",
    re.MULTILINE,
)
_PADRAO_CITACAO_TCE_SP = re.compile(
    r"([^\n]+?)\s*\n\(Sess[ãa]o(?: Plen[áa]ria)? de (\d{1,2}/\d{1,2}/\d{4})\.\s*"
    r"(?:Relatoria|Redatoria)[^:]*:\s*([^)]+)\)"
)
_PADRAO_LINK_INTEIRO_TEOR_TCE_SP = re.compile(
    r"--- links de inteiro teor desta página ---\n(https://\S+)"
)


def _fatiar_tce_sp_boletim(item_bruto_id: int, texto_bruto: str) -> list[DecisaoFatiada]:
    # o Sumário repete a citação de cada item com reticências + número de
    # página — uma linha com 5+ pontos seguidos só aparece lá, nunca no
    # corpo real; filtrar essas linhas evita duplicar cada item
    texto = _remover_linhas_com_pontilhado(texto_bruto)

    marcadores_orgao = [
        (m.start(), m.group(1)) for m in _PADRAO_ORGAO_TCE_SP.finditer(texto)
    ]
    matches = list(_PADRAO_CITACAO_TCE_SP.finditer(texto))

    decisoes = []
    for i, m in enumerate(matches):
        numero_processo = m.group(1).strip()
        data = m.group(2)
        relator = m.group(3).strip()

        orgao = None
        for pos, nome in marcadores_orgao:
            if pos > m.start():
                break
            orgao = nome

        fim = matches[i + 1].start() if i + 1 < len(matches) else len(texto)
        bloco = texto[m.start():fim]
        m_link = _PADRAO_LINK_INTEIRO_TEOR_TCE_SP.search(bloco)

        decisoes.append(DecisaoFatiada(
            item_bruto_id=item_bruto_id,
            tribunal="TCE-SP",
            numero_acordao=None,  # achado real: TCE-SP cita por processo, não acórdão
            numero_processo=numero_processo,
            orgao_julgador=orgao,
            relator=relator,
            data_julgamento=_data_br_para_iso(data),
            url_inteiro_teor=m_link.group(1).strip() if m_link else None,
            texto_decisao=bloco.strip(),
        ))
    return decisoes


def _remover_linhas_com_pontilhado(texto: str) -> str:
    """O Sumário do PDF repete a citação de cada item com reticências e
    número de página — mas quando o nome do relator é longo, a citação
    quebra em duas linhas, e só a última tem os pontinhos (a primeira
    metade escaparia de um filtro linha a linha simples). Em vez disso,
    corta tudo até a ÚLTIMA linha pontilhada do documento — é aí que o
    Sumário de fato termina e o corpo real começa."""
    linhas = texto.split("\n")
    ultimo_indice_pontilhado = None
    for i, linha in enumerate(linhas):
        if re.search(r"\.{5,}", linha):
            ultimo_indice_pontilhado = i
    if ultimo_indice_pontilhado is None:
        return texto
    return "\n".join(linhas[ultimo_indice_pontilhado + 1:])


# --- TCE-SP: súmulas (já atômico na coleta) -------------------------------

_PADRAO_NUMERO_SUMULA = re.compile(r"n\.º (\d+)")


def _fatiar_tce_sp_sumula(
    item_bruto_id: int, titulo: str, texto_bruto: str,
    url_origem: str, data_publicacao: str | None,
) -> list[DecisaoFatiada]:
    m = _PADRAO_NUMERO_SUMULA.search(titulo)
    numero_sumula = m.group(1) if m else None
    return [DecisaoFatiada(
        item_bruto_id=item_bruto_id,
        tribunal="TCE-SP",
        # súmula não é decisão de sessão — não tem número de acórdão, mas
        # é identificador citável oficial do tribunal, então reaproveita a
        # coluna (combinado explicitamente antes de implementar)
        numero_acordao=numero_sumula,
        numero_processo=None,
        orgao_julgador=None,
        relator=None,
        data_julgamento=data_publicacao,
        url_inteiro_teor=url_origem,
        texto_decisao=texto_bruto,
    )]


# --- STJ (já atômico na coleta) -------------------------------------------

_PADRAO_STJ_CITACAO = re.compile(
    r"Processo: (.+?),\s*Rel\.\s*(?:Ministro|Ministra)\s*([^,]+),\s*([^,]+),.*?"
    r"julgado em (\d{1,2}/\d{1,2}/\d{4})"
)


def _fatiar_stj(
    item_bruto_id: int, texto_bruto: str, url_origem: str, data_publicacao: str | None,
) -> list[DecisaoFatiada]:
    m = _PADRAO_STJ_CITACAO.search(texto_bruto)
    if not m:
        return [DecisaoFatiada(
            item_bruto_id=item_bruto_id, tribunal="STJ", numero_acordao=None,
            numero_processo=None, orgao_julgador=None, relator=None,
            data_julgamento=data_publicacao, url_inteiro_teor=url_origem,
            texto_decisao=texto_bruto,
        )]

    numero_processo, relator, orgao, data = m.groups()
    return [DecisaoFatiada(
        item_bruto_id=item_bruto_id,
        tribunal="STJ",
        numero_acordao=None,  # achado real: STJ cita por recurso, não acórdão
        numero_processo=numero_processo.strip(),
        orgao_julgador=_normalizar_espacos(orgao),
        relator=_normalizar_espacos(relator),
        data_julgamento=_data_br_para_iso(data),
        url_inteiro_teor=url_origem,
        texto_decisao=texto_bruto,
    )]


# --- TCU (já atômico na coleta) -------------------------------------------

_PADRAO_TCU_CITACAO = re.compile(
    # ancorado em "Citação: " de propósito — o título já repete
    # "Acórdão NNN/AAAA ÓRGÃO" antes disso, e como [^,] casa quebra de
    # linha, um regex sem essa âncora vazava do título até a vírgula da
    # linha de citação de verdade (bug real, achado no smoke test)
    r"Citação: Acórdão\s*(\d+)/(\d+)\s*([^,\n]+),.*?Relator\s*(?:Ministro|Ministra)\s*([^,.\n]+)"
)


def _fatiar_tcu(item_bruto_id: int, texto_bruto: str, url_origem: str) -> list[DecisaoFatiada]:
    m = _PADRAO_TCU_CITACAO.search(texto_bruto)
    if not m:
        return [DecisaoFatiada(
            item_bruto_id=item_bruto_id, tribunal="TCU", numero_acordao=None,
            numero_processo=None, orgao_julgador=None, relator=None,
            data_julgamento=None, url_inteiro_teor=url_origem,
            texto_decisao=texto_bruto,
        )]

    numero, ano, orgao, relator = m.groups()
    return [DecisaoFatiada(
        item_bruto_id=item_bruto_id,
        tribunal="TCU",
        numero_acordao=f"{numero}/{ano}",
        numero_processo=None,
        orgao_julgador=_normalizar_espacos(orgao),
        relator=_normalizar_espacos(relator),
        data_julgamento=None,  # não vem na CSV de dados abertos (achado da Etapa 3e)
        url_inteiro_teor=url_origem,
        texto_decisao=texto_bruto,
    )]


# --- Zênite (já atômico na coleta; sem extração de metadados) -------------

def _fatiar_zenite(item_bruto_id: int, texto_bruto: str) -> list[DecisaoFatiada]:
    return [DecisaoFatiada(
        item_bruto_id=item_bruto_id,
        tribunal=None,
        numero_acordao=None,
        numero_processo=None,
        orgao_julgador=None,
        relator=None,
        data_julgamento=None,
        url_inteiro_teor=None,
        texto_decisao=texto_bruto,
    )]


# --- Utilidades compartilhadas --------------------------------------------

def _data_br_para_iso(data_texto: str) -> str | None:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", data_texto.strip())
    if not m:
        return None
    dia, mes, ano = m.groups()
    return f"{ano}-{int(mes):02d}-{int(dia):02d}"


def _normalizar_espacos(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip()


class _ParserTextoPuro(HTMLParser):
    """Remove tags HTML remanescentes (h1/h2/p/a/b), mantendo só o texto —
    usado pra limpar o texto_decisao de TCE-PR/TCE-MG antes de guardar."""

    def __init__(self):
        super().__init__()
        self._partes: list[str] = []

    def handle_starttag(self, tag, attrs):
        del attrs
        if tag in ("p", "h1", "h2", "br"):
            self._partes.append(" ")

    def handle_data(self, data):
        self._partes.append(data)

    @property
    def resultado(self) -> str:
        return "".join(self._partes)


def _texto_puro(html_texto: str) -> str:
    parser = _ParserTextoPuro()
    parser.feed(html.unescape(html_texto))
    return _normalizar_espacos(parser.resultado)
