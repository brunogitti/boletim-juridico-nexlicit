"""Coletor da Zênite (zenite.com.br) — a fonte diária, maior volume.

Tenta wp-json → RSS → HTML da listagem, nessa ordem, cada um com retry e
backoff. O primeiro que funcionar decide os itens coletados; se os três
falharem, `coletar()` devolve um ResultadoColeta de erro em vez de levantar
exceção — falha isolada, o job principal segue pras outras fontes.

Investigação real (2026-07-31), documentada porque justifica as escolhas
abaixo:
- `wp-json/wp/v2/posts?after=...` filtra de verdade, mas contra o campo
  `date` (hora LOCAL de Brasília, sem offset), não `date_gmt`.
- `/feed/` (RSS) não pagina nem filtra por data — sempre os ~10 itens mais
  recentes. Serve de fallback de curto prazo, não cobre lacuna grande.
- `/noticias/` (HTML) só dá título/data/link na listagem; o texto do
  artigo exige uma segunda requisição por item, na própria página do post
  (`div.post-content`).
"""

import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from xml.etree import ElementTree

import requests

from nucleo.banco import inserir_item_bruto, transacao

BASE_URL = "https://zenite.com.br"
USER_AGENT = (
    "BoletimJuridicoNexLicit/0.1 (uso pessoal e não comercial; "
    "ver docs/ARQUITETURA.md)"
)
INTERVALO_ENTRE_REQUISICOES = 1.5  # segundos, cortesia com o servidor
TENTATIVAS_MAX = 3
ESPERA_INICIAL = 1.0  # segundos; dobra a cada nova tentativa
TIMEOUT_REQUISICAO = 15  # segundos
DIAS_BOOTSTRAP_PADRAO = 7  # janela usada só quando a fonte nunca foi coletada

BRASILIA = timezone(timedelta(hours=-3))  # Brasil não tem mais horário de verão

logger = logging.getLogger(__name__)


@dataclass
class ItemColetado:
    url_origem: str
    titulo: str
    data_publicacao: str  # ISO 8601 em UTC
    texto_bruto: str


@dataclass
class ResultadoColeta:
    itens_novos: int
    itens_repetidos: int
    origem: str  # "wp-json" | "rss" | "html" | "falha"
    erro: str | None = None


def coletar(conexao, fonte_id: int, *,
            dias_bootstrap: int = DIAS_BOOTSTRAP_PADRAO) -> ResultadoColeta:
    """Coleta itens novos da Zênite e grava em itens_brutos.

    Nunca levanta exceção: qualquer erro (rede, parsing, o que for) vira um
    ResultadoColeta com origem="falha", pra não derrubar o job inteiro.
    """
    try:
        return _coletar(conexao, fonte_id, dias_bootstrap)
    except Exception as erro:  # falha isolada: nada escapa daqui
        logger.warning(
            "coleta da Zênite falhou por completo",
            extra={"fonte": "zenite", "erro": str(erro)},
        )
        return ResultadoColeta(0, 0, "falha", erro=str(erro))


def _coletar(conexao, fonte_id: int, dias_bootstrap: int) -> ResultadoColeta:
    desde = _determinar_desde(conexao, fonte_id, dias_bootstrap)

    sessao = requests.Session()
    sessao.headers["User-Agent"] = USER_AGENT

    origens = (
        ("wp-json", _coletar_wp_json),
        ("rss", _coletar_rss),
        ("html", _coletar_html),
    )

    itens: list[ItemColetado] | None = None
    origem_usada = "falha"
    for nome_origem, funcao in origens:
        try:
            itens = funcao(sessao, desde)
            origem_usada = nome_origem
            break
        except Exception as erro:
            logger.warning(
                "origem da Zênite falhou, tentando a próxima",
                extra={"fonte": "zenite", "origem": nome_origem, "erro": str(erro)},
            )

    if itens is None:
        return ResultadoColeta(0, 0, "falha", erro="wp-json, rss e html falharam")

    novos = repetidos = 0
    with transacao(conexao):
        for item in itens:
            item_id = inserir_item_bruto(
                conexao,
                fonte_id=fonte_id,
                url_origem=item.url_origem,
                titulo=item.titulo,
                data_publicacao=item.data_publicacao,
                texto_bruto=item.texto_bruto,
                hash_conteudo=hashlib.sha256(
                    item.texto_bruto.encode("utf-8")
                ).hexdigest(),
            )
            if item_id is not None:
                novos += 1
            else:
                repetidos += 1

    logger.info(
        "coleta da Zênite concluída",
        extra={"fonte": "zenite", "origem": origem_usada,
               "novos": novos, "repetidos": repetidos},
    )
    return ResultadoColeta(novos, repetidos, origem_usada)


def _determinar_desde(conexao, fonte_id: int, dias_bootstrap: int) -> datetime:
    """Nunca varredura completa: usa a última data já coletada dessa fonte,
    ou uma janela de bootstrap curta se for a primeira coleta."""
    linha = conexao.execute(
        "SELECT MAX(data_publicacao) AS ultima FROM itens_brutos WHERE fonte_id = ?",
        (fonte_id,),
    ).fetchone()
    if linha and linha["ultima"]:
        return datetime.fromisoformat(linha["ultima"])
    return datetime.now(timezone.utc) - timedelta(days=dias_bootstrap)


def _requisitar(sessao: requests.Session, url: str, *,
                 params: dict | None = None) -> requests.Response:
    """GET com retry e backoff exponencial, e intervalo de cortesia sempre
    ao final (sucesso ou falha) — é o que dá o rate limit entre chamadas."""
    ultimo_erro: Exception | None = None
    for tentativa in range(TENTATIVAS_MAX):
        try:
            resposta = sessao.get(url, params=params, timeout=TIMEOUT_REQUISICAO)
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


# --- Tier 1: wp-json ---------------------------------------------------

def _coletar_wp_json(sessao: requests.Session, desde: datetime) -> list[ItemColetado]:
    # `after` filtra pelo campo `date` (hora local de Brasília, sem
    # offset) — confirmado na investigação, não é suposição.
    desde_local_naive = desde.astimezone(BRASILIA).replace(tzinfo=None).isoformat()

    itens: list[ItemColetado] = []
    pagina = 1
    while True:
        resposta = _requisitar(
            sessao,
            f"{BASE_URL}/wp-json/wp/v2/posts",
            params={
                "per_page": 20,
                "page": pagina,
                "after": desde_local_naive,
                "orderby": "date",
                "order": "asc",
            },
        )
        posts = resposta.json()
        if not posts:
            break

        for post in posts:
            itens.append(ItemColetado(
                url_origem=post["link"],
                titulo=html.unescape(post["title"]["rendered"]),
                data_publicacao=_utc_naive_para_iso(post["date_gmt"]),
                texto_bruto=_limpar_html(post["content"]["rendered"]),
            ))

        total_paginas = int(resposta.headers.get("X-WP-TotalPages", "1"))
        if pagina >= total_paginas:
            break
        pagina += 1

    return itens


# --- Tier 2: RSS ---------------------------------------------------------

_NS_CONTENT = {"content": "http://purl.org/rss/1.0/modules/content/"}


def _coletar_rss(sessao: requests.Session, desde: datetime) -> list[ItemColetado]:
    resposta = _requisitar(sessao, f"{BASE_URL}/feed/")
    raiz = ElementTree.fromstring(resposta.content)

    todos_os_itens = raiz.findall(".//item")
    if todos_os_itens:
        data_mais_antiga = min(
            _rfc822_para_datetime(item.findtext("pubDate")) for item in todos_os_itens
        )
        if data_mais_antiga > desde:
            logger.warning(
                "RSS da Zênite pode não cobrir toda a lacuna: item mais "
                "antigo do feed é mais recente que 'desde'",
                extra={"fonte": "zenite", "origem": "rss",
                       "desde": desde.isoformat(),
                       "item_mais_antigo": data_mais_antiga.isoformat()},
            )

    itens: list[ItemColetado] = []
    for item in todos_os_itens:
        data = _rfc822_para_datetime(item.findtext("pubDate"))
        if data <= desde:
            continue

        conteudo = item.find("content:encoded", _NS_CONTENT)
        texto_html = conteudo.text if conteudo is not None else (item.findtext("description") or "")

        itens.append(ItemColetado(
            url_origem=item.findtext("link") or "",
            titulo=html.unescape(item.findtext("title") or ""),
            data_publicacao=data.astimezone(timezone.utc).isoformat(),
            texto_bruto=_limpar_html(texto_html or ""),
        ))

    return itens


# --- Tier 3: HTML da listagem + página do artigo -------------------------

_MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}
_PADRAO_DATA_PTBR = re.compile(r"(\d{1,2}) de (\w+) de (\d{4})")


def _coletar_html(sessao: requests.Session, desde: datetime) -> list[ItemColetado]:
    resposta = _requisitar(sessao, f"{BASE_URL}/noticias/")
    candidatos = _parsear_listagem(resposta.text)

    itens: list[ItemColetado] = []
    for link, titulo, data in candidatos:
        if data <= desde:
            continue
        resposta_artigo = _requisitar(sessao, link)
        texto = _extrair_post_content(resposta_artigo.text)
        itens.append(ItemColetado(
            url_origem=link,
            titulo=html.unescape(titulo),
            data_publicacao=data.astimezone(timezone.utc).isoformat(),
            texto_bruto=_limpar_html(texto),
        ))

    return itens


class _ParserListagem(HTMLParser):
    """Extrai (link, título, data) de cada bloco <div class="post-item">."""

    def __init__(self):
        super().__init__()
        self.itens: list[tuple[str, str, str]] = []
        self._link_atual: str | None = None
        self._em_date = False
        self._em_titulo = False
        self._data_texto = ""
        self._titulo_texto = ""

    def handle_starttag(self, tag, attrs):
        atributos = dict(attrs)
        if tag == "a" and "href" in atributos and self._link_atual is None:
            self._link_atual = atributos["href"]
        elif tag == "div" and atributos.get("class") == "date":
            self._em_date = True
            self._data_texto = ""
        elif tag == "h3" and "post-title" in (atributos.get("class") or ""):
            self._em_titulo = True
            self._titulo_texto = ""

    def handle_data(self, data):
        if self._em_date:
            self._data_texto += data
        elif self._em_titulo:
            self._titulo_texto += data

    def handle_endtag(self, tag):
        if tag == "div" and self._em_date:
            self._em_date = False
        elif tag == "h3" and self._em_titulo:
            self._em_titulo = False
            if self._link_atual is not None:
                self.itens.append((
                    self._link_atual,
                    self._titulo_texto.strip(),
                    self._data_texto.strip(),
                ))
            self._link_atual = None


def _parsear_listagem(html_texto: str) -> list[tuple[str, str, datetime]]:
    parser = _ParserListagem()
    parser.feed(html_texto)

    candidatos = []
    for link, titulo, data_texto in parser.itens:
        data = _data_ptbr_para_datetime(data_texto)
        if data is not None:
            candidatos.append((link, titulo, data))
    return candidatos


class _ParserPostContent(HTMLParser):
    """Extrai o HTML interno de <div class="post-content">, parando no
    fechamento da própria div (rastreando profundidade de <div> aninhada)."""

    def __init__(self):
        super().__init__()
        self._dentro = False
        self._profundidade = 0
        self._partes: list[str] = []

    def handle_starttag(self, tag, attrs):
        atributos = dict(attrs)
        if not self._dentro and tag == "div" and atributos.get("class") == "post-content":
            self._dentro = True
            self._profundidade = 1
            return
        if self._dentro:
            if tag == "div":
                self._profundidade += 1
            self._partes.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        if not self._dentro:
            return
        if tag == "div":
            self._profundidade -= 1
            if self._profundidade == 0:
                self._dentro = False
                return
        self._partes.append(f"</{tag}>")

    def handle_data(self, data):
        if self._dentro:
            self._partes.append(data)

    @property
    def resultado(self) -> str:
        return "".join(self._partes)


def _extrair_post_content(html_texto: str) -> str:
    parser = _ParserPostContent()
    parser.feed(html_texto)
    return parser.resultado


def _data_ptbr_para_datetime(texto: str) -> datetime | None:
    m = _PADRAO_DATA_PTBR.search(texto.lower())
    if not m:
        return None
    dia, nome_mes, ano = m.groups()
    mes = _MESES_PT.get(nome_mes)
    if mes is None:
        return None
    return datetime(int(ano), mes, int(dia), tzinfo=BRASILIA)


# --- Utilidades de data e limpeza de HTML --------------------------------

def _utc_naive_para_iso(data_gmt: str) -> str:
    """`date_gmt` do wp-json vem sem offset (ex: 2026-07-30T06:00:00), já em UTC."""
    return datetime.fromisoformat(data_gmt).replace(tzinfo=timezone.utc).isoformat()


def _rfc822_para_datetime(pub_date: str | None) -> datetime:
    if not pub_date:
        raise ValueError("pubDate ausente no item do RSS")
    return parsedate_to_datetime(pub_date)


class _ParserTexto(HTMLParser):
    def __init__(self):
        super().__init__()
        self._partes: list[str] = []

    def handle_starttag(self, tag, attrs):
        del attrs  # não usado; nome mantido igual ao da classe base
        if tag == "p" and self._partes:
            self._partes.append("\n\n")

    def handle_data(self, data):
        self._partes.append(data)

    @property
    def resultado(self) -> str:
        return "".join(self._partes)


def _limpar_html(html_texto: str) -> str:
    """HTML de content.rendered/content:encoded/post-content -> texto puro.

    Tags fora, entidades decodificadas, parágrafos separados por linha em
    branco, espaços redundantes colapsados.
    """
    parser = _ParserTexto()
    parser.feed(html.unescape(html_texto))
    texto = parser.resultado
    texto = re.sub(r"[ \t]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()
