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
    # String curta pra diferenciar itens nas tabelas de log/triagem — não é
    # o título do boletim (Etapa 7, ver docs/ARQUITETURA.md Camada 6), só um
    # apoio de leitura enquanto calibramos a triagem. TCE-PR usa a própria
    # linha de citação (já extraída, mais rica que qualquer coisa que a
    # gente monte). As fontes sem linha de citação própria usam o padrão
    # genérico de _identificador_padrao. None quando a fonte ainda não
    # populou (TCE-SP, Zênite — não pedido; TCE-MG — aguardando fix da
    # segmentação por tribunal).
    identificador_exibicao: str | None = None


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
        return _fatiar_zenite(item_bruto_id, texto_bruto, url_origem)
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
            # a própria linha de citação do TCE-PR já é o identificador
            # mais completo que existe — número de processo, acórdão,
            # órgão, relator e data numa frase só
            identificador_exibicao=_texto_puro(m.group(0)),
        ))
        inicio = m.end()
    return decisoes


# --- TCE-MG --------------------------------------------------------------
#
# Achado real (2026-08-07, smoke test contra os 19 itens já coletados): o
# cabeçalho de seção NÃO é <h2> (isso só existe pro breadcrumb e pra data
# da edição). O nome do tribunal aparece de dois jeitos, os dois reais,
# confirmados contra páginas ao vivo (edições 333 e 334):
#   variante 1: <p><a></a>Nome do Tribunal</p>            (âncora vazia)
#   variante 2: <p><b><a>Nome do Tribunal</a></b></p>     (âncora envolve o texto)
# As duas nunca têm href — os <a href="#tN">...</a> que aparecem antes são
# o sumário/índice da própria edição, não o cabeçalho de verdade, e por
# isso são excluídos explicitamente do padrão abaixo.
#
# Além disso, o traço da citação própria ("Processo <a...>ID</a> – Tipo...")
# é travessão (–, U+2013), não hífen (-, U+002D) — o fixture antigo (feito à
# mão, não HTML real) tinha os dois errados, o que escondeu os dois bugs.
#
# **Limitação conhecida, aceita por ora** (ver docs/ARQUITETURA.md, Riscos):
# edições anteriores à 332 usam uma estrutura de sumário totalmente
# diferente (organizada por colegiado — "Tribunal Pleno", "Segunda
# Câmara" — em vez de por tribunal) e a citação própria nem usa o formato
# "Processo <a...>" nessas edições. Não cobrimos essa variante agora;
# esses itens ficam com status='erro' (fatiar_item devolve lista vazia,
# rodar_triagem.py trata isso como falha, não como sucesso silencioso).
#
# Achado real (2026-08-10, lendo a tabela de descartes da triagem): até
# aqui, texto_decisao de TCE-MG era só a linha de citação (~100
# caracteres) — nem a citação própria nem a de outro tribunal carregam o
# parágrafo que descreve o caso de verdade. Esse parágrafo existe na
# página (confirmado olhando a fonte real), antes da citação, mas nunca
# era capturado. O separador visual "* * * * * *" marca o início de cada
# item de verdade (inclusive descartando o metadado de busca — "ATENÇÃO:
# ...", "Palavras-chave: ...", "Processos relacionados: ..." — que sobra
# no fim do item anterior e não pode vazar pro início do próximo).

_MAPEAMENTO_TRIBUNAL_TCE_MG = {
    "tribunal de contas do estado de minas gerais": "TCE-MG",
    "tribunal de contas da união": "TCU",
    "supremo tribunal federal": "STF",
    "superior tribunal de justiça": "STJ",
    "tribunal de justiça de minas gerais": "TJMG",
}

_PADRAO_CABECALHO_TCE_MG = re.compile(
    r"<p>\s*(?:<b>)?\s*<a(?![^>]*\bhref\b)[^>]*>([^<]*)</a>(?:</b>)?\s*([^<]*?)\s*</p>"
)

_PADRAO_TCE_MG_PROPRIO = re.compile(
    r'<p>Processo\s*<a href="([^"]+)">([^<]+)</a>\s*[-–]\s*'
    r'[^.]+\.\s*'  # tipo de processo — sem coluna própria, descartado
    r'([^.]+)\.\s*'
    r'(?:Sess[ãa]o de|Deliberado em)\s*(\d{1,2}/\d{1,2}/\d{4})\.?'
    r'(?:\s*Publicado no DOC em \d{1,2}/\d{1,2}/\d{4}\.?)?'
    r'\s*Rel\.\s*([^<]+?)\s*</p>'
)
_PADRAO_TCE_MG_OUTRO_TRIBUNAL = re.compile(
    # "Acórdão" e "N/AAAA Órgão" às vezes vêm em duas tags <b> separadas
    # (resíduo de exportação do Word) em vez de uma só — achado real,
    # edição 333 tem as duas variantes na mesma página
    r"<b>Acórdão(?:</b>\s*<b>)?\s*(\d+/\d+)\s*([^<]+?)</b>\s*\([^,]+,\s*Relator\s*"
    r"(?:Ministro|Ministra)\s*([^)]+)\)"
)
_PADRAO_SEPARADOR_ITEM_TCE_MG = re.compile(r"\*\s+\*\s+\*\s+\*\s+\*\s+\*")


def _fatiar_com_contexto(segmento: str, padrao_citacao: re.Pattern):
    """Pra cada citação, devolve (match, bloco) onde bloco é o texto desde
    o separador visual "* * * * * *" mais próximo antes da citação (ou do
    início do segmento, se não houver separador antes) até o fim da
    própria citação. É nesse trecho — antes da citação, não nela — que
    mora a descrição real do caso."""
    limites = [0] + [m.end() for m in _PADRAO_SEPARADOR_ITEM_TCE_MG.finditer(segmento)]
    resultado = []
    for m in padrao_citacao.finditer(segmento):
        candidatos = [limite for limite in limites if limite <= m.start()]
        inicio = max(candidatos) if candidatos else 0
        resultado.append((m, segmento[inicio:m.end()]))
    return resultado


def _fatiar_tce_mg(item_bruto_id: int, texto_bruto: str) -> list[DecisaoFatiada]:
    decisoes = []
    for tribunal, segmento in _dividir_por_secao_tce_mg(texto_bruto):
        if tribunal == "TCE-MG":
            for m, bloco in _fatiar_com_contexto(segmento, _PADRAO_TCE_MG_PROPRIO):
                url, numero_processo, orgao, data, relator = m.groups()
                numero_processo = numero_processo.strip()
                relator = _normalizar_espacos(relator)
                data_iso = _data_br_para_iso(data)
                decisoes.append(DecisaoFatiada(
                    item_bruto_id=item_bruto_id,
                    tribunal="TCE-MG",
                    numero_acordao=None,  # achado real: TCE-MG não cita acórdão aqui
                    numero_processo=numero_processo,
                    orgao_julgador=_normalizar_espacos(orgao),
                    relator=relator,
                    data_julgamento=data_iso,
                    url_inteiro_teor=html.unescape(url),
                    texto_decisao=_texto_puro(bloco),
                    identificador_exibicao=_identificador_padrao(
                        numero_acordao=None, numero_processo=numero_processo,
                        relator=relator, data_julgamento=data_iso,
                    ),
                ))
        else:
            for m, bloco in _fatiar_com_contexto(segmento, _PADRAO_TCE_MG_OUTRO_TRIBUNAL):
                numero_acordao, orgao, relator = m.groups()
                relator = _normalizar_espacos(relator)
                decisoes.append(DecisaoFatiada(
                    item_bruto_id=item_bruto_id,
                    tribunal=tribunal,
                    numero_acordao=numero_acordao,
                    numero_processo=None,
                    orgao_julgador=_normalizar_espacos(orgao),
                    relator=relator,
                    data_julgamento=None,  # não informado nesse formato
                    url_inteiro_teor=None,  # achado real: sem link nesses trechos
                    texto_decisao=_texto_puro(bloco),
                    identificador_exibicao=_identificador_padrao(
                        numero_acordao=numero_acordao, numero_processo=None,
                        relator=relator, data_julgamento=None,
                    ),
                ))
    return decisoes


def _dividir_por_secao_tce_mg(texto_bruto: str) -> list[tuple[str, str]]:
    """Separa a edição em blocos por cabeçalho de seção real (as duas
    variantes de _PADRAO_CABECALHO_TCE_MG). Cabeçalho que não bate com
    nome de tribunal conhecido (ex. "DESTAQUE", "Ementas por Área
    Temática") não troca o tribunal corrente — só reorganiza o mesmo
    conteúdo por dentro, então o bloco seguinte continua valendo pro
    tribunal que já estava em vigor. Tudo antes do primeiro cabeçalho
    reconhecido é implicitamente TCE-MG (é onde a introdução/sumário
    ficam, mas eles não geram decisão nenhuma, então não tem risco de
    atribuição errada)."""
    partes = _PADRAO_CABECALHO_TCE_MG.split(texto_bruto)
    segmentos = [("TCE-MG", partes[0])]
    tribunal_atual = "TCE-MG"
    i = 1
    while i < len(partes):
        texto_cabecalho = (partes[i] + partes[i + 1]).strip().lower()
        conteudo = partes[i + 2] if i + 2 < len(partes) else ""
        tribunal_atual = _MAPEAMENTO_TRIBUNAL_TCE_MG.get(texto_cabecalho, tribunal_atual)
        segmentos.append((tribunal_atual, conteudo))
        i += 3
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
            identificador_exibicao=_identificador_padrao(
                numero_acordao=None, numero_processo=None,
                relator=None, data_julgamento=data_publicacao,
            ),
        )]

    numero_processo, relator, orgao, data = m.groups()
    numero_processo = numero_processo.strip()
    relator = _normalizar_espacos(relator)
    data_iso = _data_br_para_iso(data)
    return [DecisaoFatiada(
        item_bruto_id=item_bruto_id,
        tribunal="STJ",
        numero_acordao=None,  # achado real: STJ cita por recurso, não acórdão
        numero_processo=numero_processo,
        orgao_julgador=_normalizar_espacos(orgao),
        relator=relator,
        data_julgamento=data_iso,
        url_inteiro_teor=url_origem,
        texto_decisao=texto_bruto,
        identificador_exibicao=_identificador_padrao(
            numero_acordao=None, numero_processo=numero_processo,
            relator=relator, data_julgamento=data_iso,
        ),
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
            identificador_exibicao=_identificador_padrao(
                numero_acordao=None, numero_processo=None,
                relator=None, data_julgamento=None,
            ),
        )]

    numero, ano, orgao, relator = m.groups()
    numero_acordao = f"{numero}/{ano}"
    relator = _normalizar_espacos(relator)
    return [DecisaoFatiada(
        item_bruto_id=item_bruto_id,
        tribunal="TCU",
        numero_acordao=numero_acordao,
        numero_processo=None,
        orgao_julgador=_normalizar_espacos(orgao),
        relator=relator,
        data_julgamento=None,  # não vem na CSV de dados abertos (achado da Etapa 3e)
        url_inteiro_teor=url_origem,
        texto_decisao=texto_bruto,
        identificador_exibicao=_identificador_padrao(
            numero_acordao=numero_acordao, numero_processo=None,
            relator=relator, data_julgamento=None,
        ),
    )]


# --- Zênite (já atômico na coleta; sem extração de metadados) -------------

def _fatiar_zenite(item_bruto_id: int, texto_bruto: str, url_origem: str) -> list[DecisaoFatiada]:
    # achado real (2026-08-10): diferente de tribunal/acórdão/relator (que
    # exigem extrair algo do texto livre, com risco real de inventar),
    # url_inteiro_teor não precisa ser extraído — url_origem já é a fonte
    # original de verdade pra uma notícia da Zênite. Deixar None aqui
    # tirava a Zênite inteira do e-mail pra sempre (regra de âncora,
    # Camada 5), sem necessidade — mesma lógica que STJ já usa quando uma
    # nota não tem link próprio (cai pra URL da edição).
    return [DecisaoFatiada(
        item_bruto_id=item_bruto_id,
        tribunal=None,
        numero_acordao=None,
        numero_processo=None,
        orgao_julgador=None,
        relator=None,
        data_julgamento=None,
        url_inteiro_teor=url_origem,
        texto_decisao=texto_bruto,
    )]


# --- Utilidades compartilhadas --------------------------------------------

def _identificador_padrao(
    *, numero_acordao: str | None, numero_processo: str | None,
    relator: str | None, data_julgamento: str | None,
) -> str:
    """Identificador de exibição pras fontes sem linha de citação própria
    (TCE-MG, STJ, TCU): número (acórdão de preferência, senão processo) +
    relator quando houver, senão + data. Só um apoio de leitura nas
    tabelas de log/triagem — não é o título do boletim (Camada 6)."""
    if numero_acordao:
        numero = f"Acórdão {numero_acordao}"
    elif numero_processo:
        numero = f"Processo {numero_processo}"
    else:
        numero = "sem identificador"

    if relator:
        return f"{numero} — Rel. {relator}"
    if data_julgamento:
        return f"{numero} — {data_julgamento}"
    return numero


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
