"""Coletor do TCE-SP — Boletim de Jurisprudência (mensal) e Súmulas.

Duas fontes de conteúdo bem diferentes sob a mesma `fonte_id` (a
arquitetura já trata "Boletim de Jurisprudência + Súmulas" como uma única
fonte, seção 2): o boletim é PDF mensal, as súmulas são uma lista HTML
única que nunca pagina.

Investigação real (2026-08-04), documentada porque justifica o desenho:
- `/boletim-de-jurisprudencia` é só uma landing page. A listagem de
  verdade fica em `/boletim-de-jurisprudencia/publicacoes` (paginada,
  `?page=0,1,2...`) — achada seguindo o card "Boletins Anteriores", não
  por adivinhação. URLs de edição mudam de padrão no meio do histórico
  (`/boletim-de-jurisprudencia/publicacoes/{slug}` nas recentes,
  `/publicacoes/{slug}` nas antigas) — nunca montamos URL na mão.
- **A página de cada edição não tem nenhum conteúdo em HTML** — só capa,
  data de publicação (`<time datetime="...">`, já em ISO) e um link pro
  PDF. `docs/ARQUITETURA.md` já registrava isso (`TCE-SP | HTML/PDF`).
  Testado com PyMuPDF: texto extrai limpo (não é imagem escaneada), e o
  PDF cita cada decisão por **número de processo** (nunca "Acórdão n.º"),
  com o link de inteiro teor embutido como hyperlink do PDF, não visível
  no texto — por isso anexamos os links de cada página logo depois do
  texto dela, pra não perder essa informação.
- `/boletim-de-jurisprudencia/sumulas` traz as 52 súmulas inteiras numa
  página só, cada uma com um botão que abre um modal Bootstrap **que já
  vem no mesmo HTML** (não é AJAX) com HISTÓRICO (resoluções de
  aprovação/alteração/cancelamento, com data) e FUNDAMENTO. Cada súmula
  vira um item independente em itens_brutos — é o que faz "súmula nova ou
  alterada" funcionar: só a que mudou de verdade gera linha nova (as
  outras batem no hash e caem em ON CONFLICT DO NOTHING).

Sobre "vale como item de impacto alto por padrão": itens_brutos não tem
coluna impacto (só existe em decisoes, preenchida na Etapa 6). O que este
coletor faz é marcar toda súmula com título prefixado "Súmula TCE-SP
n.º X", pra ficar trivial reconhecer e aplicar a regra quando a
triagem/análise existirem.
"""

import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

import fitz
import requests

from nucleo.banco import inserir_item_bruto, transacao
from nucleo.coleta_comum import USER_AGENT

BASE_URL = "https://www.tce.sp.gov.br"
BOLETIM_LISTAGEM_URL = f"{BASE_URL}/boletim-de-jurisprudencia/publicacoes"
SUMULAS_URL = f"{BASE_URL}/boletim-de-jurisprudencia/sumulas"
INTERVALO_ENTRE_REQUISICOES = 1.5  # segundos, cortesia com o servidor
TENTATIVAS_MAX = 3
ESPERA_INICIAL = 1.0  # segundos; dobra a cada nova tentativa
TIMEOUT_REQUISICAO = 15  # segundos
LIMITE_PADRAO = 5  # edições novas do boletim por execução; é mensal, não precisa de mais
MAX_PAGINAS_LISTAGEM = 5  # trava de segurança na paginação da listagem

BRASILIA = timezone(timedelta(hours=-3))  # Brasil não tem mais horário de verão

logger = logging.getLogger(__name__)


@dataclass
class EdicaoIndice:
    url: str
    titulo: str


@dataclass
class ItemColetado:
    url_origem: str
    titulo: str
    data_publicacao: str | None
    texto_bruto: str


@dataclass
class ResultadoColeta:
    itens_novos: int
    itens_repetidos: int
    erro: str | None = None


def coletar(conexao, fonte_id: int, *,
            limite_boletim: int = LIMITE_PADRAO) -> ResultadoColeta:
    """Coleta súmulas novas/alteradas e edições novas do boletim do TCE-SP.

    Nunca levanta exceção: qualquer erro vira um ResultadoColeta com
    `erro` preenchido, pra não derrubar o job inteiro.
    """
    try:
        return _coletar(conexao, fonte_id, limite_boletim)
    except Exception as erro:  # falha isolada: nada escapa daqui
        logger.warning(
            "coleta do TCE-SP falhou por completo",
            extra={"fonte": "tce_sp", "erro": str(erro)},
        )
        return ResultadoColeta(0, 0, erro=str(erro))


def _coletar(conexao, fonte_id: int, limite_boletim: int) -> ResultadoColeta:
    sessao = requests.Session()
    sessao.headers["User-Agent"] = USER_AGENT

    novos = repetidos = 0
    erros: list[str] = []

    itens_sumulas: list[ItemColetado] = []
    try:
        itens_sumulas = _coletar_sumulas(sessao)
    except Exception as erro:
        logger.warning("falha ao coletar súmulas",
                        extra={"fonte": "tce_sp", "erro": str(erro)})
        erros.append(f"súmulas: {erro}")

    for item in itens_sumulas:
        if _gravar_item(conexao, fonte_id, item):
            novos += 1
        else:
            repetidos += 1

    pendentes: list[EdicaoIndice] = []
    try:
        ja_coletadas = _edicoes_ja_coletadas(conexao, fonte_id)
        pendentes = _listar_edicoes_pendentes(sessao, ja_coletadas)[:limite_boletim]
    except Exception as erro:
        logger.warning("falha ao listar edições do boletim",
                        extra={"fonte": "tce_sp", "erro": str(erro)})
        erros.append(f"índice do boletim: {erro}")

    for edicao in pendentes:
        try:
            item = _coletar_edicao_boletim(sessao, edicao)
        except Exception as erro:
            # falha isolada por edição: um PDF quebrado não pode impedir
            # a coleta das outras
            logger.warning(
                "falha ao coletar uma edição do boletim, seguindo pras próximas",
                extra={"fonte": "tce_sp", "url": edicao.url, "erro": str(erro)},
            )
            erros.append(f"{edicao.url}: {erro}")
            continue

        if _gravar_item(conexao, fonte_id, item):
            novos += 1
        else:
            repetidos += 1

    logger.info(
        "coleta do TCE-SP concluída",
        extra={"fonte": "tce_sp", "novos": novos, "repetidos": repetidos,
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


def _edicoes_ja_coletadas(conexao, fonte_id: int) -> set[str]:
    linhas = conexao.execute(
        "SELECT url_origem FROM itens_brutos WHERE fonte_id = ?", (fonte_id,)
    ).fetchall()
    return {linha["url_origem"] for linha in linhas}


def _requisitar(sessao: requests.Session, url: str) -> requests.Response:
    """GET com retry e backoff exponencial, e intervalo de cortesia sempre
    ao final (sucesso ou falha) — é o que dá o rate limit entre chamadas.
    Serve tanto pra HTML quanto pro PDF (o chamador decide .text ou .content)."""
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


# --- Súmulas (HTML único, sem paginação) ------------------------------------

_PADRAO_SUMULA = re.compile(
    r'<p><b>S[ÚU]MULA N[º°ª]\s*(\d+)\s*(?:&nbsp;)?\s*</b>\s*-\s*(?:&nbsp;)?\s*(.*?)</p>',
    re.DOTALL,
)
_PADRAO_DATA_DOE = re.compile(
    r"(?:Aprovada|Alterada|Cancelada) pela Resolu[çc][ãa]o[^(]*\(DOE de "
    r"(?P<dia>\d{2})/(?P<mes>\d{2})/(?P<ano>\d{4})\)"
)


def _coletar_sumulas(sessao: requests.Session) -> list[ItemColetado]:
    resposta = _requisitar(sessao, SUMULAS_URL)
    pagina = resposta.text

    itens: list[ItemColetado] = []
    for m in _PADRAO_SUMULA.finditer(pagina):
        numero, corpo_html = m.groups()
        cancelada = "<s>" in corpo_html or "CANCELADA" in corpo_html
        # o corpo inclui o link "(Veja histórico e fundamento)" no fim;
        # ele já aparece de novo (com conteúdo de verdade) na seção
        # HISTÓRICO E FUNDAMENTO logo abaixo, então corta daqui.
        corpo_sem_link = re.sub(r'<a[^>]*>\(Veja[^<]*\)</a>\s*$', '', corpo_html.strip())
        ementa = _texto_puro(corpo_sem_link)

        modal_html = _extrair_div_por_id(pagina, f"sum-modal-{numero}")
        historico_fundamento = _texto_puro(modal_html)

        titulo = f"Súmula TCE-SP n.º {numero}"
        if cancelada:
            titulo += " (CANCELADA)"

        texto_bruto = (
            f"{titulo}\n\n{ementa}\n\n"
            f"HISTÓRICO E FUNDAMENTO:\n{historico_fundamento}"
        ).strip()

        itens.append(ItemColetado(
            url_origem=f"{SUMULAS_URL}#sum-modal-{numero}",
            titulo=titulo,
            data_publicacao=_ultima_data_doe(historico_fundamento),
            texto_bruto=texto_bruto,
        ))

    return itens


def _ultima_data_doe(texto: str) -> str | None:
    datas = [
        datetime(int(m["ano"]), int(m["mes"]), int(m["dia"]), tzinfo=BRASILIA)
        for m in _PADRAO_DATA_DOE.finditer(texto)
    ]
    if not datas:
        return None
    return max(datas).astimezone(timezone.utc).isoformat()


# --- Boletim de Jurisprudência (PDF mensal, listagem paginada) -------------

_PADRAO_PUBLICACAO = re.compile(
    r'<div class="publicacao"><a href="([^"]+)">.*?alt="([^"]*)"',
    re.DOTALL,
)
_PADRAO_DATA_PUBLICACAO = re.compile(r'<time datetime="([^"]+)">')
_PADRAO_LINK_PDF = re.compile(
    r'href="(https://www\.tce\.sp\.gov\.br/sites/default/files/publicacoes/[^"]+?\.pdf)"'
)


def _listar_edicoes_pendentes(
    sessao: requests.Session, ja_coletadas: set[str],
    *, max_paginas: int = MAX_PAGINAS_LISTAGEM,
) -> list[EdicaoIndice]:
    """Percorre a listagem paginada e devolve só as edições que ainda não
    estão em itens_brutos. Para de paginar assim que uma página não trouxer
    nada novo — mesmo desenho do coletores/tce_mg.py."""
    pendentes: list[EdicaoIndice] = []
    for pagina in range(max_paginas):
        url_pagina = (
            BOLETIM_LISTAGEM_URL if pagina == 0
            else f"{BOLETIM_LISTAGEM_URL}?page={pagina}"
        )
        resposta = _requisitar(sessao, url_pagina)

        itens_pagina = _PADRAO_PUBLICACAO.findall(resposta.text)
        if not itens_pagina:
            break  # página vazia: acabou a listagem

        novos_na_pagina = [
            EdicaoIndice(url=href, titulo=html.unescape(alt).strip())
            for href, alt in itens_pagina
            if href not in ja_coletadas
        ]
        pendentes.extend(novos_na_pagina)
        if len(novos_na_pagina) < len(itens_pagina):
            break  # essa página já tinha edição conhecida -> fim da lacuna

    return pendentes


def _coletar_edicao_boletim(sessao: requests.Session,
                             edicao: EdicaoIndice) -> ItemColetado:
    resposta_pagina = _requisitar(sessao, edicao.url)

    m_data = _PADRAO_DATA_PUBLICACAO.search(resposta_pagina.text)
    data_publicacao = _normalizar_data_iso(m_data.group(1)) if m_data else None

    m_pdf = _PADRAO_LINK_PDF.search(resposta_pagina.text)
    if not m_pdf:
        raise ValueError(f"não encontrei link de PDF na página da edição")
    url_pdf = html.unescape(m_pdf.group(1))

    resposta_pdf = _requisitar(sessao, url_pdf)
    texto_bruto = _extrair_texto_pdf(resposta_pdf.content)

    return ItemColetado(
        url_origem=edicao.url,
        titulo=edicao.titulo,
        data_publicacao=data_publicacao,
        texto_bruto=texto_bruto,
    )


def _normalizar_data_iso(valor: str) -> str:
    return datetime.fromisoformat(valor.replace("Z", "+00:00")).isoformat()


_DOMINIO_INTEIRO_TEOR = "https://jurisprudencia.tce.sp.gov.br"


def _extrair_texto_pdf(conteudo_pdf: bytes) -> str:
    """Texto de cada página + os links de inteiro teor daquela página logo
    em seguida — o PyMuPDF não coloca o link inline no texto, e associar
    cada link ao processo certo é trabalho do fatiador (Etapa 4), não
    deste coletor. Aqui só garantimos que a informação não se perde."""
    documento = fitz.open(stream=conteudo_pdf, filetype="pdf")
    try:
        partes = []
        for pagina in documento:
            texto = pagina.get_text()
            links = [
                link["uri"] for link in pagina.get_links()
                if link.get("uri", "").startswith(_DOMINIO_INTEIRO_TEOR)
            ]
            partes.append(texto)
            if links:
                partes.append(
                    "--- links de inteiro teor desta página ---\n"
                    + "\n".join(links)
                )
        return "\n\n".join(parte.strip() for parte in partes if parte.strip())
    finally:
        documento.close()


# --- Utilidades de limpeza de HTML ------------------------------------------

class _ParserDivPorId(HTMLParser):
    """Extrai o HTML interno de <div id="{alvo}">, rastreando profundidade
    de <div> aninhado pra achar o fechamento certo."""

    def __init__(self, alvo_id: str):
        super().__init__()
        self._alvo_id = alvo_id
        self._dentro = False
        self._profundidade = 0
        self._partes: list[str] = []

    def handle_starttag(self, tag, attrs):
        atributos = dict(attrs)
        if not self._dentro:
            if tag == "div" and atributos.get("id") == self._alvo_id:
                self._dentro = True
                self._profundidade = 1
            return
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


def _extrair_div_por_id(html_texto: str, alvo_id: str) -> str:
    parser = _ParserDivPorId(alvo_id)
    parser.feed(html_texto)
    return parser.resultado


class _ParserTextoPuro(HTMLParser):
    """Remove todas as tags, mantendo só o texto — usado onde não há link
    nenhum pra preservar (súmulas não têm link de inteiro teor por item)."""

    def __init__(self):
        super().__init__()
        self._partes: list[str] = []

    def handle_starttag(self, tag, attrs):
        del attrs
        if tag in ("p", "br", "tr"):
            self._partes.append("\n")

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
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()
