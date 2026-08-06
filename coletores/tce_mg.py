"""Coletor do TCE-MG — Informativo de Jurisprudência.

Página índice descoberta por investigação (não havia link nenhum óbvio):
dentro da própria página de uma edição existe um bloco "Links úteis" com
`<a href=".../Noticia/?cod_secao=1ISP...">Informativo de Jurisprudência</a>`
— o breadcrumb do site, por outro lado, é só texto (`<a disabled>`), não
leva a lugar nenhum. Nunca varremos por incremento de ID: a lista de
edições sempre vem dessa página índice.

Investigação real (2026-08-04), documentada porque justifica o desenho:
- `https://www.tce.mg.gov.br/Noticia/?cod_secao=1ISP&tipo=1&url=&cod_secao_menu=5L`
  lista as edições mais recentes (19 por página, com data, título e link
  já no formato `/Informativo-de-Jurisprudencia-n-{numero}.html/Noticia/{id}`
  — a mesma URL do exemplo original). Páginas seguintes:
  `.../Noticia/Index/?paginacao={n}&cod_secao=1ISP&tipo=1&url=&cod_secao_menu=5L`,
  confirmado até a página 2 continuando exatamente de onde a 1 parou.
- Cada edição mistura DOIS formatos de citação bem diferentes: as decisões
  do próprio TCE-MG vêm como `(Processo {ID} – {TIPO}. {ÓRGÃO}. Rel. {NOME}.
  Deliberado em D/M/AAAA)` (ou "Sessão de D/M/AAAA" em edições mais novas),
  com o número do processo linkado a `tcjuris.tce.mg.gov.br` ou
  `mapjuris.tce.mg.gov.br` — mas **nunca cita número de acórdão**. Já os
  trechos selecionados de outros tribunais (STF/STJ/TCU/TJMG, que a própria
  edição descreve reproduzir) citam `Acórdão {NÚMERO}/{ANO} {ÓRGÃO}
  (Relator Ministro {NOME})` — só que **sem nenhum link**. Ou seja: quem
  tem link não tem número de acórdão, e quem tem número de acórdão não tem
  link. Isso é relevante pra regra de âncora do boletim (Etapa 4/triagem),
  não pra este coletor — aqui só registro o achado.
- O conteúdo de cada edição fica em `<div class="conteudo">`, confirmado
  em duas edições (283 e 333) com HTML de origem bem diferente uma da
  outra (a 283 parece exportação de Word "clássica"; a 333 tem cara de
  editor colaborativo tipo Word Online, com classes `SCXW...`) — mesmo
  desembrulhador de HTML do TCE-PR aguenta os dois.
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
    "https://www.tce.mg.gov.br/Noticia/"
    "?cod_secao=1ISP&tipo=1&url=&cod_secao_menu=5L"
)
INDICE_URL_PAGINACAO = (
    "https://www.tce.mg.gov.br/Noticia/Index/"
    "?paginacao={pagina}&cod_secao=1ISP&tipo=1&url=&cod_secao_menu=5L"
)
BASE_URL = "https://www.tce.mg.gov.br"
INTERVALO_ENTRE_REQUISICOES = 1.5  # segundos, cortesia com o servidor
TENTATIVAS_MAX = 3
ESPERA_INICIAL = 1.0  # segundos; dobra a cada nova tentativa
TIMEOUT_REQUISICAO = 15  # segundos
LIMITE_PADRAO = 10  # edições novas por execução; nunca varredura completa
MAX_PAGINAS_INDICE = 5  # trava de segurança na paginação do índice

BRASILIA = timezone(timedelta(hours=-3))  # Brasil não tem mais horário de verão

logger = logging.getLogger(__name__)


@dataclass
class InformativoIndice:
    numero: str
    url: str
    titulo: str
    data_publicacao: str  # ISO 8601 em UTC, vem direto do índice


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
    """Coleta edições novas do Informativo do TCE-MG e grava em itens_brutos.

    Nunca levanta exceção: qualquer erro vira um ResultadoColeta com
    `erro` preenchido, pra não derrubar o job inteiro.
    """
    try:
        return _coletar(conexao, fonte_id, limite_por_execucao)
    except Exception as erro:  # falha isolada: nada escapa daqui
        logger.warning(
            "coleta do TCE-MG falhou por completo",
            extra={"fonte": "tce_mg", "erro": str(erro)},
        )
        return ResultadoColeta(0, 0, erro=str(erro))


def _coletar(conexao, fonte_id: int, limite_por_execucao: int) -> ResultadoColeta:
    sessao = requests.Session()
    sessao.headers["User-Agent"] = USER_AGENT

    ja_coletados = _informativos_ja_coletados(conexao, fonte_id)
    pendentes = _listar_informativos_pendentes(sessao, ja_coletados)[:limite_por_execucao]

    novos = repetidos = 0
    erros: list[str] = []
    for informativo in pendentes:
        try:
            item = _coletar_informativo(sessao, informativo)
        except Exception as erro:
            # falha isolada por edição: uma página quebrada não pode
            # impedir a coleta das outras
            logger.warning(
                "falha ao coletar uma edição, seguindo pras próximas",
                extra={"fonte": "tce_mg", "url": informativo.url, "erro": str(erro)},
            )
            erros.append(f"{informativo.url}: {erro}")
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
        "coleta do TCE-MG concluída",
        extra={"fonte": "tce_mg", "novos": novos, "repetidos": repetidos,
               "falhas": len(erros)},
    )
    erro_final = "; ".join(erros) if erros and novos == 0 and repetidos == 0 else None
    return ResultadoColeta(novos, repetidos, erro=erro_final)


def _informativos_ja_coletados(conexao, fonte_id: int) -> set[str]:
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


# --- Página índice (paginada) ----------------------------------------------

# O <a> de cada item nunca fecha antes do </h2> (bug real do HTML deles).
_PADRAO_LINK_INFORMATIVO = re.compile(
    r'<span class="data-noticia-internas">(\d{2})/(\d{2})/(\d{4})\s*-?\s*</span>'
    r'<a href="([^"]+)"[^>]*>([^<]*)</h2>'
)
_PADRAO_NUMERO_URL = re.compile(r"Informativo-de-Jurisprudencia-n-(\d+)\.html")


def _listar_informativos_pendentes(
    sessao: requests.Session, ja_coletados: set[str],
    *, max_paginas: int = MAX_PAGINAS_INDICE,
) -> list[InformativoIndice]:
    """Percorre o índice paginado e devolve só as edições que ainda não
    estão em itens_brutos. Para de paginar assim que uma página inteira
    não trouxer nada novo — sinal de que alcançamos o que já foi
    sincronizado, sem precisar continuar gastando requisição."""
    pendentes: list[InformativoIndice] = []
    for pagina in range(1, max_paginas + 1):
        url_pagina = INDICE_URL if pagina == 1 else INDICE_URL_PAGINACAO.format(pagina=pagina)
        resposta = _requisitar(sessao, url_pagina)

        itens_pagina = list(_PADRAO_LINK_INFORMATIVO.finditer(resposta.text))
        if not itens_pagina:
            break  # página vazia: acabou o índice

        novos_na_pagina = []
        for m in itens_pagina:
            dia, mes, ano, href, titulo = m.groups()
            url_absoluta = urljoin(BASE_URL, href)
            if url_absoluta in ja_coletados:
                continue
            m_numero = _PADRAO_NUMERO_URL.search(href)
            numero = m_numero.group(1) if m_numero else ""
            data_local = datetime(int(ano), int(mes), int(dia), tzinfo=BRASILIA)
            novos_na_pagina.append(InformativoIndice(
                numero=numero,
                url=url_absoluta,
                titulo=html.unescape(titulo).strip(),
                data_publicacao=data_local.astimezone(timezone.utc).isoformat(),
            ))

        pendentes.extend(novos_na_pagina)
        if len(novos_na_pagina) < len(itens_pagina):
            break  # essa página já tinha edição conhecida -> fim da lacuna

    return pendentes


# --- Página de uma edição ---------------------------------------------------

def _coletar_informativo(sessao: requests.Session,
                          informativo: InformativoIndice) -> ItemColetado:
    resposta = _requisitar(sessao, informativo.url)
    conteudo = _extrair_conteudo_principal(resposta.text)
    return ItemColetado(
        url_origem=informativo.url,
        titulo=informativo.titulo,
        data_publicacao=informativo.data_publicacao,
        texto_bruto=conteudo,
    )


# --- Desembrulhador de HTML -------------------------------------------------
# Mesmo desenho do coletores/tce_pr.py: mantém h1/h2/p/a[href]/b, ignora o
# resto (inclusive conteúdo de <style>/<script>). Duplicado de propósito —
# ainda não existe um módulo compartilhado entre coletores.

_TAGS_MANTIDAS = {"h1", "h2", "p", "a", "b"}
_TAGS_VAZIAS = {"br", "img", "hr", "meta", "link", "input"}
_TAGS_IGNORAR_CONTEUDO = {"style", "script"}


class _ParserConteudoPrincipal(HTMLParser):
    """Extrai o conteúdo de <div class="conteudo">, mantendo só
    h1/h2/p/a[href]/b e descartando o resto."""

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
            if tag == "div" and atributos.get("class") == "conteudo":
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
