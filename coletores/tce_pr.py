"""Coletor do TCE-PR — Boletim Informativo de Jurisprudência.

Página índice lista os boletins (nunca adivinhamos número por sequência —
o padrão de URL muda no meio do histórico, ver abaixo). Cada boletim vira
UM item em itens_brutos; quebrar em decisões individuais é trabalho do
fatiador (Etapa 4), não deste coletor.

Investigação real (2026-08-04), documentada porque justifica o desenho:
- A página índice (`.../boletim-informativo-de-jurisprudencia/`) tem 179
  boletins, de 2017 a 2026, numa página só (sem paginação, sem JS). A URL
  de cada boletim muda de padrão no meio da lista: edições recentes usam
  `.../conteudo/boletim-...-tce-pr-n-{numero}-{ano}.htm`; edições mais
  antigas usam `.../conteudo/boletim-...-tce-pr-n-{numero}-{ano}/{id}/area/242/`.
  A ordem da lista não é estritamente numérica (é ordem de publicação real
  no CMS deles) — mais um motivo pra nunca montar a URL por conta própria.
- A linha de citação no fim de cada decisão segue o padrão
  `(TIPO n.º NNNNNN/AAAA, Acórdão n.º XXXX/AAAA, ÓRGÃO, Rel. NOME,
  julgado em DD/MM/AAAA, veiculado em DD/MM/AAAA no DETC)`, com "Acórdão
  n.º XXXX/AAAA" dentro de um link para o ViaJuris — é esse link que vira
  o inteiro teor exigido pela regra de âncora.
- Duas edições, dois templates de HTML completamente diferentes (169-183
  ainda são exportação do Word, cheia de <span style="..."> aninhado; a
  184 em diante usa <blockquote>/<p class="bij-reference"> mais limpo, e
  embute um <style> de ~50KB dentro do próprio <main>). Por isso o
  desembrulhador de HTML abaixo ignora conteúdo de <style>/<script> e não
  assume que o cabeçalho do item é sempre <h1> — a 184 usa <p role="heading">.
"""

import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

from nucleo.banco import inserir_item_bruto, transacao
from nucleo.coleta_comum import USER_AGENT

INDICE_URL = (
    "https://www.tce.pr.gov.br/fiscalizado/informativos-do-tcepr/"
    "boletim-informativo-de-jurisprudencia/"
)
INTERVALO_ENTRE_REQUISICOES = 1.5  # segundos, cortesia com o servidor
TENTATIVAS_MAX = 3
ESPERA_INICIAL = 1.0  # segundos; dobra a cada nova tentativa
TIMEOUT_REQUISICAO = 15  # segundos
LIMITE_PADRAO = 10  # boletins novos por execução; nunca varredura completa

BRASILIA = timezone(timedelta(hours=-3))  # Brasil não tem mais horário de verão

logger = logging.getLogger(__name__)


@dataclass
class BoletimIndice:
    numero: str
    ano: str
    url: str
    titulo: str


@dataclass
class ItemColetado:
    url_origem: str
    titulo: str
    data_publicacao: str | None  # ISO 8601 em UTC; None quando não estimável
    texto_bruto: str


@dataclass
class ResultadoColeta:
    itens_novos: int
    itens_repetidos: int
    erro: str | None = None


def coletar(conexao, fonte_id: int, *,
            limite_por_execucao: int = LIMITE_PADRAO) -> ResultadoColeta:
    """Coleta boletins novos do TCE-PR e grava em itens_brutos.

    Nunca levanta exceção: qualquer erro vira um ResultadoColeta com
    `erro` preenchido, pra não derrubar o job inteiro.
    """
    try:
        return _coletar(conexao, fonte_id, limite_por_execucao)
    except Exception as erro:  # falha isolada: nada escapa daqui
        logger.warning(
            "coleta do TCE-PR falhou por completo",
            extra={"fonte": "tce_pr", "erro": str(erro)},
        )
        return ResultadoColeta(0, 0, erro=str(erro))


def _coletar(conexao, fonte_id: int, limite_por_execucao: int) -> ResultadoColeta:
    sessao = requests.Session()
    sessao.headers["User-Agent"] = USER_AGENT

    indice = _listar_boletins_indice(sessao)
    ja_coletados = _boletins_ja_coletados(conexao, fonte_id)
    pendentes = [b for b in indice if b.url not in ja_coletados][:limite_por_execucao]

    novos = repetidos = 0
    erros: list[str] = []
    for boletim in pendentes:
        try:
            item = _coletar_boletim(sessao, boletim)
        except Exception as erro:
            # falha isolada por boletim: um HTML mal-formado não pode
            # impedir a coleta dos outros
            logger.warning(
                "falha ao coletar um boletim, seguindo pros próximos",
                extra={"fonte": "tce_pr", "url": boletim.url, "erro": str(erro)},
            )
            erros.append(f"{boletim.url}: {erro}")
            continue

        with transacao(conexao):
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
        "coleta do TCE-PR concluída",
        extra={"fonte": "tce_pr", "novos": novos, "repetidos": repetidos,
               "falhas": len(erros)},
    )
    erro_final = "; ".join(erros) if erros and novos == 0 and repetidos == 0 else None
    return ResultadoColeta(novos, repetidos, erro=erro_final)


def _boletins_ja_coletados(conexao, fonte_id: int) -> set[str]:
    linhas = conexao.execute(
        "SELECT url_origem FROM itens_brutos WHERE fonte_id = ?", (fonte_id,)
    ).fetchall()
    return {linha["url_origem"] for linha in linhas}


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


# --- Página índice --------------------------------------------------------

# Casa os dois padrões de URL reais (".../tce-pr-n-184-2026.htm" e
# ".../tce-pr-n-168-2025/365744/area/242/") a partir do miolo em comum.
_PADRAO_LINK_BOLETIM = re.compile(
    r'<a href="([^"]*tce-pr-n-(\d+)-(\d{4})[^"]*)">([^<]+)</a>'
)


def _listar_boletins_indice(sessao: requests.Session) -> list[BoletimIndice]:
    """Lê a página índice e devolve os boletins na ordem em que aparecem
    (não reordenamos por número — o site não segue sequência estrita)."""
    resposta = _requisitar(sessao, INDICE_URL)

    vistos: set[str] = set()
    boletins: list[BoletimIndice] = []
    for m in _PADRAO_LINK_BOLETIM.finditer(resposta.text):
        href_relativo, numero, ano, titulo = m.groups()
        url_absoluta = urljoin(INDICE_URL, href_relativo)
        if url_absoluta in vistos:
            continue  # a seção "Último Boletim" repete o 1º item da lista
        vistos.add(url_absoluta)
        boletins.append(BoletimIndice(
            numero=numero, ano=ano, url=url_absoluta,
            titulo=html.unescape(titulo).strip(),
        ))
    return boletins


# --- Página de um boletim --------------------------------------------------

def _coletar_boletim(sessao: requests.Session, boletim: BoletimIndice) -> ItemColetado:
    resposta = _requisitar(sessao, boletim.url)
    conteudo = _extrair_conteudo_principal(resposta.text)
    return ItemColetado(
        url_origem=boletim.url,
        titulo=boletim.titulo,
        data_publicacao=_estimar_data_publicacao(conteudo),
        texto_bruto=conteudo,
    )


_PADRAO_VEICULADO = re.compile(r"veiculado em (\d{2})/(\d{2})/(\d{4})")


def _estimar_data_publicacao(texto: str) -> str | None:
    """Não há uma data única confiável pro boletim inteiro (cada decisão
    tem a sua). Usa a mais recente das datas "veiculado em" como
    aproximação — melhor-esforço, o campo é opcional no schema."""
    datas = [
        datetime(int(ano), int(mes), int(dia), tzinfo=BRASILIA)
        for dia, mes, ano in _PADRAO_VEICULADO.findall(texto)
    ]
    if not datas:
        return None
    return max(datas).astimezone(timezone.utc).isoformat()


# --- Desembrulhador de HTML -------------------------------------------------

_TAGS_MANTIDAS = {"h1", "h2", "p", "a", "b"}
_TAGS_VAZIAS = {"br", "img", "hr", "meta", "link", "input"}
_TAGS_IGNORAR_CONTEUDO = {"style", "script"}


class _ParserConteudoPrincipal(HTMLParser):
    """Extrai o conteúdo de <main class="ready-search-details ...">,
    mantendo só h1/h2/p/a[href]/b e descartando o resto (os <span> de
    estilo do Word, ou qualquer outra tag de layout) — sem depender de
    BeautifulSoup. Ignora por completo o conteúdo de <style>/<script>."""

    def __init__(self):
        super().__init__()
        self._dentro = False
        self._profundidade = 0
        self._pilha_emitida: list[str] = []
        self._ignorando_desde: int | None = None
        self._partes: list[str] = []

    def handle_starttag(self, tag, attrs):
        atributos = dict(attrs)
        if not self._dentro:
            if tag == "main" and "ready-search-details" in (atributos.get("class") or ""):
                self._dentro = True
                self._profundidade = 1
            return

        if tag in _TAGS_VAZIAS:
            if self._ignorando_desde is None and tag in _TAGS_MANTIDAS:
                self._partes.append(f"<{tag}>")
            return

        self._profundidade += 1

        if self._ignorando_desde is not None:
            return
        if tag in _TAGS_IGNORAR_CONTEUDO:
            self._ignorando_desde = self._profundidade
            return
        if tag in _TAGS_MANTIDAS:
            href = atributos.get("href") if tag == "a" else None
            if href:
                self._partes.append(f'<a href="{html.escape(href, quote=True)}">')
            else:
                self._partes.append(f"<{tag}>")
            self._pilha_emitida.append(tag)

    def handle_endtag(self, tag):
        if not self._dentro:
            return
        if tag in _TAGS_VAZIAS:
            return

        self._profundidade -= 1

        if self._ignorando_desde is not None:
            if self._profundidade < self._ignorando_desde:
                self._ignorando_desde = None
            if self._profundidade == 0:
                self._dentro = False
            return

        if self._pilha_emitida and self._pilha_emitida[-1] == tag:
            self._pilha_emitida.pop()
            self._partes.append(f"</{tag}>")
            if tag in ("p", "h1", "h2"):
                self._partes.append("\n")

        if self._profundidade == 0:
            self._dentro = False

    def handle_data(self, data):
        if self._dentro and self._ignorando_desde is None:
            self._partes.append(data)

    @property
    def resultado(self) -> str:
        return "".join(self._partes)


def _extrair_conteudo_principal(html_texto: str) -> str:
    parser = _ParserConteudoPrincipal()
    parser.feed(html_texto)
    texto = parser.resultado
    texto = re.sub(r"[ \t\xa0]+", " ", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()
