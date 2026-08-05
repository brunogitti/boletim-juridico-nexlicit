"""Coletor do STJ — Informativo de Jurisprudência, filtrado por Direito
Administrativo.

A página de Últimas Notícias do STJ é SharePoint com renderização por
JavaScript e volta vazia (confirmado, não serve). O Informativo de
Jurisprudência funciona: HTML renderizado no servidor (JSP), sem JS
necessário.

Investigação real (2026-08-05), documentada porque justifica o desenho:
- `https://scon.stj.jus.br/jurisprudencia/externo/informativo/` mostra a
  edição mais recente. A própria página esconde
  (`style="display:none"`) um link de feed Atom —
  `https://ww2.stj.jus.br/jurisprudencia/externo/InformativoFeed` — que
  funciona (200 OK após 2 redirects) e tem **928 entradas**, cobrindo o
  histórico completo, cada uma com número, data (`<updated>`, já ISO
  8601) e a URL exata da edição. Muito mais confiável que tentar montar
  URL incrementando número.
- Periodicidade **semanal** pras edições regulares, confirmada pelas
  datas do próprio feed (887→888→889... cada uma 7 dias depois), com
  hiato em julho por recesso forense. Existe uma numeração **separada**
  pras edições extraordinárias/especiais (sufixo "E" no id do feed, ex.
  "INFJ0033E") — este coletor ignora essas, só processa as regulares.
- Cada edição é uma lista de "notas" (teses), cada uma com três campos:
  Processo (citação + link real pra `processo.stj.jus.br`), Ramo do
  Direito (pode vir combinado, ex. "DIREITO ADMINISTRATIVO, DIREITO
  PROCESSUAL CIVIL" — por isso o filtro é substring, não igualdade) e
  Tema (o texto da tese). Sem "Acórdão n.º" — cita por tipo de recurso +
  número (ex. "AgInt no REsp 2.162.500-RJ").
- **Nem toda nota tem link**: achei um caso real, "Processo em segredo de
  justiça, Rel. Ministro..." — sem nenhum link público. Guardamos a nota
  mesmo assim (é conteúdo real e útil pro registro histórico), mas o
  `url_origem` cai pra URL da própria edição nesse caso.
- Cada nota já é uma unidade pequena e autocontida — não precisa de
  fatiador aqui, cada uma vira direto um item em itens_brutos (mesma
  lógica das súmulas do TCE-SP).
- Sobre o filtro "dentro de Direito Administrativo, licitação e
  contratos administrativos": só o primeiro filtro (Ramo do Direito) é
  aplicado aqui, por ser um campo estrutural confiável. O recorte fino
  por licitação/contratos fica pra Etapa 5 (triagem via LLM) — um filtro
  de palavra-chave aqui arriscaria descartar item relevante que não usa
  exatamente essas palavras.
"""

import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests

from nucleo.banco import inserir_item_bruto, transacao

FEED_URL = "https://ww2.stj.jus.br/jurisprudencia/externo/InformativoFeed"
# O Cloudflare do STJ devolve 403 pra User-Agent com caractere acentuado
# (confirmado isolando a variável: mesma requisição, só trocando "não"
# por "nao" já resolve — cabeçalho HTTP não é UTF-8, e um UA com byte
# multibyte cru é sinal clássico de tráfego anômalo pra esse WAF). Por
# isso, diferente dos outros coletores, este aqui evita acento no UA.
USER_AGENT = (
    "BoletimJuridicoNexLicit/0.1 (uso pessoal e nao comercial; "
    "ver docs/ARQUITETURA.md)"
)
INTERVALO_ENTRE_REQUISICOES = 1.5  # segundos, cortesia com o servidor
TENTATIVAS_MAX = 3
ESPERA_INICIAL = 1.0  # segundos; dobra a cada nova tentativa
TIMEOUT_REQUISICAO = 15  # segundos
LIMITE_PADRAO = 5  # edições novas por execução; é semanal, não precisa de mais
BOOTSTRAP_EDICOES = 3  # na primeira coleta, quantas edições regulares pra trás

RAMO_ALVO = "DIREITO ADMINISTRATIVO"

logger = logging.getLogger(__name__)


@dataclass
class EdicaoFeed:
    numero: str  # só dígitos, sem sufixo "E" de extraordinária
    data_publicacao: str  # ISO 8601 em UTC, já vem limpo do feed
    url: str


@dataclass
class ItemColetado:
    url_origem: str
    titulo: str
    data_publicacao: str
    texto_bruto: str


@dataclass
class ResultadoColeta:
    itens_novos: int
    itens_repetidos: int
    erro: str | None = None


def coletar(conexao, fonte_id: int, *,
            limite_por_execucao: int = LIMITE_PADRAO) -> ResultadoColeta:
    """Coleta notas de Direito Administrativo das edições novas do
    Informativo de Jurisprudência do STJ.

    Nunca levanta exceção: qualquer erro vira um ResultadoColeta com
    `erro` preenchido, pra não derrubar o job inteiro.
    """
    try:
        return _coletar(conexao, fonte_id, limite_por_execucao)
    except Exception as erro:  # falha isolada: nada escapa daqui
        logger.warning(
            "coleta do STJ falhou por completo",
            extra={"fonte": "stj", "erro": str(erro)},
        )
        return ResultadoColeta(0, 0, erro=str(erro))


def _coletar(conexao, fonte_id: int, limite_por_execucao: int) -> ResultadoColeta:
    sessao = requests.Session()
    sessao.headers["User-Agent"] = USER_AGENT

    edicoes = _listar_edicoes_feed(sessao)  # mais recente primeiro
    maior_coletada = _maior_edicao_coletada(conexao, fonte_id)

    if maior_coletada is None:
        pendentes = edicoes[:BOOTSTRAP_EDICOES]
    else:
        pendentes = [e for e in edicoes if int(e.numero) > maior_coletada]
        pendentes = pendentes[:limite_por_execucao]

    pendentes.reverse()  # processa da mais antiga pendente pra mais nova

    novos = repetidos = 0
    erros: list[str] = []
    for edicao in pendentes:
        try:
            itens = _coletar_edicao(sessao, edicao)
        except Exception as erro:
            # falha isolada por edição: uma página quebrada não pode
            # impedir a coleta das outras
            logger.warning(
                "falha ao coletar uma edição, seguindo pras próximas",
                extra={"fonte": "stj", "edicao": edicao.numero, "erro": str(erro)},
            )
            erros.append(f"edição {edicao.numero}: {erro}")
            continue

        for item in itens:
            if _gravar_item(conexao, fonte_id, item):
                novos += 1
            else:
                repetidos += 1

    logger.info(
        "coleta do STJ concluída",
        extra={"fonte": "stj", "novos": novos, "repetidos": repetidos,
               "falhas": len(erros)},
    )
    erro_final = "; ".join(erros) if erros and novos == 0 and repetidos == 0 else None
    return ResultadoColeta(novos, repetidos, erro=erro_final)


def _gravar_item(conexao, fonte_id: int, item: ItemColetado) -> bool:
    with transacao(conexao):
        item_id = inserir_item_bruto(
            conexao,
            fonte_id=fonte_id,
            url_origem=item.url_origem,
            titulo=item.titulo,
            data_publicacao=item.data_publicacao,
            texto_bruto=item.texto_bruto,
            hash_conteudo=hashlib.sha256(item.texto_bruto.encode("utf-8")).hexdigest(),
        )
    return item_id is not None


_PADRAO_TITULO_EDICAO = re.compile(r"STJ Informativo n\. (\d+)")


def _maior_edicao_coletada(conexao, fonte_id: int) -> int | None:
    """Não existe uma tabela separada de "edições coletadas" — cada nota
    é o seu próprio item, então a maior edição já vista é lida de volta
    dos títulos já gravados."""
    linhas = conexao.execute(
        "SELECT titulo FROM itens_brutos WHERE fonte_id = ?", (fonte_id,)
    ).fetchall()
    numeros = [
        int(m.group(1))
        for linha in linhas
        if (m := _PADRAO_TITULO_EDICAO.search(linha["titulo"]))
    ]
    return max(numeros) if numeros else None


def _requisitar(sessao: requests.Session, url: str) -> requests.Response:
    """GET com retry e backoff exponencial, e intervalo de cortesia sempre
    ao final (sucesso ou falha) — é o que dá o rate limit entre chamadas."""
    ultimo_erro: Exception | None = None
    for tentativa in range(TENTATIVAS_MAX):
        try:
            resposta = sessao.get(url, timeout=TIMEOUT_REQUISICAO)
            resposta.raise_for_status()
            return resposta
        except requests.RequestException as erro:
            ultimo_erro = erro
            if tentativa < TENTATIVAS_MAX - 1:
                time.sleep(ESPERA_INICIAL * (2 ** tentativa))
        finally:
            time.sleep(INTERVALO_ENTRE_REQUISICOES)
    assert ultimo_erro is not None
    raise ultimo_erro


# --- Feed Atom ---------------------------------------------------------

# Só casa id puramente numérico (INFJ0895) — o sufixo "E" das
# extraordinárias (INFJ0033E) quebra o \d{4} seguido de </id> e a entrada
# é pulada, de propósito.
_PADRAO_ENTRY = re.compile(
    r"<entry>\s*"
    r"<id>[^<]*INFJ(?P<numero>\d{4})</id>\s*"
    r"<title>[^<]*</title>\s*"
    r'<link href="(?P<url>[^"]+)"\s*/>\s*'
    r"<summary[^>]*></summary>\s*"
    r"<updated>(?P<data>[^<]+)</updated>\s*"
    r"</entry>",
)


def _listar_edicoes_feed(sessao: requests.Session) -> list[EdicaoFeed]:
    resposta = _requisitar(sessao, FEED_URL)
    edicoes = []
    for m in _PADRAO_ENTRY.finditer(resposta.text):
        edicoes.append(EdicaoFeed(
            numero=m.group("numero"),
            data_publicacao=_normalizar_data_iso(m.group("data")),
            url=html.unescape(m.group("url")),
        ))
    return edicoes


def _normalizar_data_iso(valor: str) -> str:
    return datetime.fromisoformat(valor).astimezone(timezone.utc).isoformat()


# --- Página de uma edição -----------------------------------------------

def _coletar_edicao(sessao: requests.Session, edicao: EdicaoFeed) -> list[ItemColetado]:
    resposta = _requisitar(sessao, edicao.url)
    return _extrair_notas_administrativas(resposta.text, edicao)


_PADRAO_NOTA = re.compile(
    r'clsIdentificaProcesso">.*?'
    # "Processo" vem com espaço/quebra de linha antes do </div> na página
    # real (o rótulo tem uma checkbox de exportação embutida antes do
    # texto) — meu primeiro fixture, simplificado à mão, não tinha esse
    # espaço, e só o teste contra o site de verdade pegou isso.
    r'Processo\s*</div>\s*<div class="divCell clsInformativoTexto">(?P<processo>.*?)</div>\s*</div>\s*'
    r'<div class="divLinha">\s*<div class="divCell clsInformativoLabel">Ramo do Direito</div>\s*'
    r'<div class="divCell clsInformativoTexto"><p>(?P<ramo>[^<]*)</p></div>\s*</div>.*?'
    r"<span>Tema</span>.*?"
    r'<div class="divCell clsInformativoTexto"><p[^>]*>(?P<tema>.*?)</p></div>',
    re.DOTALL,
)
_PADRAO_LINK_PROCESSO = re.compile(
    r'href="(https://processo\.stj\.jus\.br/processo/pesquisa/[^"]+)"'
)


def _extrair_notas_administrativas(
    html_texto: str, edicao: EdicaoFeed,
) -> list[ItemColetado]:
    itens: list[ItemColetado] = []
    for m in _PADRAO_NOTA.finditer(html_texto):
        ramo = m.group("ramo")
        if RAMO_ALVO not in ramo:
            continue

        processo_html = m.group("processo")
        citacao = _texto_puro(processo_html)
        tema = _texto_puro(m.group("tema"))

        m_link = _PADRAO_LINK_PROCESSO.search(processo_html)
        # nem toda nota tem link (processo em segredo de justiça, por
        # exemplo) — cai pra URL da própria edição nesse caso
        url_origem = html.unescape(m_link.group(1)) if m_link else edicao.url

        resumo_citacao = citacao.split(",")[0].strip()
        # sem zero à esquerda no título (edicao.numero é "0895"; o site
        # mostra "n. 895") — o zero à esquerda continua servindo pra
        # montar a URL de cada edição, só não aparece pro leitor
        titulo = f"STJ Informativo n. {int(edicao.numero)} — {resumo_citacao}"

        texto_bruto = (
            f"{titulo}\n\n"
            f"Processo: {citacao}\n"
            f"Ramo do Direito: {ramo}\n\n"
            f"Tema:\n{tema}"
        ).strip()

        itens.append(ItemColetado(
            url_origem=url_origem,
            titulo=titulo,
            data_publicacao=edicao.data_publicacao,
            texto_bruto=texto_bruto,
        ))

    return itens


# --- Utilidade de limpeza de HTML ------------------------------------------

class _ParserTextoPuro(HTMLParser):
    """Remove todas as tags, mantendo só o texto — o link do processo já
    foi extraído à parte antes de chamar isso, não precisa preservar."""

    def __init__(self):
        super().__init__()
        self._partes: list[str] = []

    def handle_starttag(self, tag, attrs):
        del attrs
        if tag in ("p", "br"):
            self._partes.append(" ")

    def handle_data(self, data):
        self._partes.append(data)

    @property
    def resultado(self) -> str:
        return "".join(self._partes)


def _texto_puro(html_texto: str) -> str:
    parser = _ParserTextoPuro()
    parser.feed(html.unescape(html_texto))
    texto = parser.resultado
    texto = re.sub(r"[ \t\xa0]+", " ", texto)
    return texto.strip()
