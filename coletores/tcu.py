"""Coletor do TCU — Informativo de Licitações e Contratos e Boletim de
Jurisprudência, via base de dados abertos (CSV). Sem PDF, sem Word.

Investigação real (2026-08-05), documentada porque justifica o desenho:
- A listagem em `portal.tcu.gov.br/jurisprudencia` é um SPA em Next.js: o
  HTML servido pelo servidor não contém nenhuma linha de publicação nem
  link de PDF/Word — tudo isso é buscado pelo navegador depois, via uma
  API que não consegui identificar sem inspecionar o app rodando de
  verdade. Por isso não construímos nada em cima dessa página.
- Em vez disso, `docs/ARQUITETURA.md` já mandava verificar a base de dados
  abertos antes de escrever qualquer parser de PDF — e ela tem as duas
  publicações inteiras, em CSV, desde 2013:
    - `.../arquivos/boletim-informativo-lc/boletim-informativo-lc.csv`
      (Informativo de Licitações e Contratos, quinzenal)
    - `.../arquivos/boletim-jurisprudencia/boletim-jurisprudencia.csv`
      (Boletim de Jurisprudência, semanal)
  Cada arquivo vem ordenado da edição mais recente pra mais antiga.
  Testado: nenhum PDF supera essa estrutura (nada de OCR, nada de layout
  de página) — por isso este coletor não usa PyMuPDF, apesar de a
  dependência já estar no projeto por causa do TCE-SP.
- O campo `TEXTOACORDAO` de cada linha já traz número do acórdão, ano e
  colegiado numa tag XML embutida:
  `<acordao_decisao_tcu colegiado="X" numero="Y" ano="Z">Acórdão Y/Z
  X</acordao_decisao_tcu>, {tipo de processo}, Relator {nome}`. Cerca de
  5% das linhas da CSV da LC (concentradas em anos antigos, 2016-2017)
  vêm com esse campo vazio — são puladas, sem número de acórdão não dá
  pra montar item válido.
- **Nenhuma das duas CSVs tem link nem data de publicação.** Pro link,
  a base de dados abertos também documenta um webservice de acórdãos
  (`dados-abertos.apps.tcu.gov.br/api/acordao/recupera-acordaos`) que
  devolve um campo `urlAcordao` pronto — mas indexado por uma chave
  interna diferente da chave das CSVs de boletim, e sem filtro por
  número/ano (só pagina em bloco). Em vez de mais uma chamada HTTP por
  item, construímos o link de busca oficial do TCU a partir de
  colegiado+número+ano (testado, responde 200 — é a mesma interface de
  busca oficial, `pesquisa.apps.tcu.gov.br`). Pra data, `data_publicacao`
  fica `null`: não existe no dado de origem, e o schema já prevê o campo
  como opcional — nada de estimar.
- **Risco conhecido, não resolvido nesta etapa**: a CSV da LC estava com
  `Last-Modified` de ~6 semanas antes da investigação, mais que o dobro
  do intervalo quinzenal esperado — pode estar defasada em relação à
  publicação real. Combinado com o usuário: seguir com a CSV mesmo assim
  e monitorar (a coleta vai mostrar a mesma "edição mais recente" por
  semanas seguidas se isso acontecer, o que é fácil de perceber depois).
"""

import csv
import hashlib
import io
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

from nucleo.banco import inserir_item_bruto, transacao
from nucleo.coleta_comum import USER_AGENT

LC_CSV_URL = (
    "https://sites.tcu.gov.br/dados-abertos/jurisprudencia/arquivos/"
    "boletim-informativo-lc/boletim-informativo-lc.csv"
)
BOLETIM_CSV_URL = (
    "https://sites.tcu.gov.br/dados-abertos/jurisprudencia/arquivos/"
    "boletim-jurisprudencia/boletim-jurisprudencia.csv"
)
BUSCA_ACORDAO_URL_BASE = (
    "https://pesquisa.apps.tcu.gov.br/documento/acordao-completo/*/{query}/"
    "DTRELEVANCIA%20desc,%20NUMACORDAOINT%20desc/0"
)

INTERVALO_ENTRE_REQUISICOES = 1.5  # segundos, cortesia com o servidor
TENTATIVAS_MAX = 3
ESPERA_INICIAL = 1.0  # segundos; dobra a cada nova tentativa
TIMEOUT_REQUISICAO = 30  # segundos — as CSVs têm alguns MB
LIMITE_PADRAO = 15  # itens novos por publicação por execução
BOOTSTRAP_EDICOES = 2  # edições regulares pra trás na primeira coleta, por publicação

logger = logging.getLogger(__name__)


@dataclass
class ItemColetado:
    url_origem: str
    titulo: str
    data_publicacao: str | None  # nenhuma das duas CSVs traz data
    texto_bruto: str


@dataclass
class ResultadoColeta:
    itens_novos: int
    itens_repetidos: int
    erro: str | None = None


def coletar(conexao, fonte_id: int, *,
            limite_por_publicacao: int = LIMITE_PADRAO) -> ResultadoColeta:
    """Coleta itens novos do Informativo de Licitações e Contratos e do
    Boletim de Jurisprudência do TCU.

    Nunca levanta exceção: qualquer erro vira um ResultadoColeta com
    `erro` preenchido, pra não derrubar o job inteiro.
    """
    try:
        return _coletar(conexao, fonte_id, limite_por_publicacao)
    except Exception as erro:  # falha isolada: nada escapa daqui
        logger.warning(
            "coleta do TCU falhou por completo",
            extra={"fonte": "tcu", "erro": str(erro)},
        )
        return ResultadoColeta(0, 0, erro=str(erro))


def _coletar(conexao, fonte_id: int, limite: int) -> ResultadoColeta:
    sessao = requests.Session()
    sessao.headers["User-Agent"] = USER_AGENT

    novos = repetidos = 0
    erros: list[str] = []

    publicacoes = (
        ("Informativo de Licitações e Contratos", LC_CSV_URL,
         "TCU Informativo LC", _PADRAO_TITULO_LC),
        ("Boletim de Jurisprudência", BOLETIM_CSV_URL,
         "TCU Boletim Jurisprudência", _PADRAO_TITULO_BOLETIM),
    )
    for nome, url, prefixo, padrao_titulo in publicacoes:
        try:
            n, r = _coletar_publicacao(
                conexao, sessao, fonte_id, url, prefixo, padrao_titulo, limite,
            )
            novos += n
            repetidos += r
        except Exception as erro:
            # falha isolada por publicação: se a CSV da LC falhar, a do
            # Boletim segue normal
            logger.warning(
                "falha ao coletar uma publicação do TCU, seguindo pra próxima",
                extra={"fonte": "tcu", "publicacao": nome, "erro": str(erro)},
            )
            erros.append(f"{nome}: {erro}")

    logger.info(
        "coleta do TCU concluída",
        extra={"fonte": "tcu", "novos": novos, "repetidos": repetidos,
               "falhas": len(erros)},
    )
    erro_final = "; ".join(erros) if erros and novos == 0 and repetidos == 0 else None
    return ResultadoColeta(novos, repetidos, erro=erro_final)


def _coletar_publicacao(
    conexao, sessao: requests.Session, fonte_id: int, url: str,
    titulo_prefixo: str, padrao_titulo: re.Pattern, limite: int,
) -> tuple[int, int]:
    ultima_edicao = _maior_edicao_coletada(conexao, fonte_id, padrao_titulo)

    resposta = _requisitar(sessao, url)
    texto_csv = resposta.content.decode("utf-8-sig")
    leitor = csv.DictReader(io.StringIO(texto_csv), delimiter="|")

    linhas_pendentes = _linhas_pendentes(leitor, ultima_edicao, limite)

    novos = repetidos = 0
    for linha in linhas_pendentes:
        item = _montar_item(linha, titulo_prefixo)
        if item is None:
            continue  # sem TEXTOACORDAO parseável, sem âncora possível
        if _gravar_item(conexao, fonte_id, item):
            novos += 1
        else:
            repetidos += 1

    return novos, repetidos


def _linhas_pendentes(leitor: csv.DictReader, ultima_edicao: int | None,
                       limite: int) -> list[dict]:
    """O arquivo vem ordenado da edição mais recente pra mais antiga. Para
    de ler assim que a edição já bate com a última coletada; na primeira
    coleta (ultima_edicao is None), processa só as BOOTSTRAP_EDICOES mais
    recentes — nunca varredura completa do histórico desde 2013."""
    pendentes: list[dict] = []
    edicoes_vistas: set[int] = set()

    for linha in leitor:
        m_edicao = _PADRAO_EDICAO_NUMERO.search(linha.get("TITULO", ""))
        if not m_edicao:
            continue
        numero_edicao = int(m_edicao.group(1))

        if ultima_edicao is not None:
            if numero_edicao <= ultima_edicao:
                break
        elif numero_edicao not in edicoes_vistas and len(edicoes_vistas) >= BOOTSTRAP_EDICOES:
            break

        edicoes_vistas.add(numero_edicao)
        pendentes.append(linha)
        if len(pendentes) >= limite:
            break

    return pendentes


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


_PADRAO_TITULO_LC = re.compile(r"TCU Informativo LC (\d+)/")
_PADRAO_TITULO_BOLETIM = re.compile(r"TCU Boletim Jurisprud\w+ (\d+)/")


def _maior_edicao_coletada(conexao, fonte_id: int, padrao_titulo: re.Pattern) -> int | None:
    """Não existe tabela separada de "edições coletadas" — cada linha da
    CSV é o seu próprio item, então a maior edição já vista é lida de
    volta dos títulos já gravados (mesma ideia do coletores/stj.py)."""
    linhas = conexao.execute(
        "SELECT titulo FROM itens_brutos WHERE fonte_id = ?", (fonte_id,)
    ).fetchall()
    numeros = [
        int(m.group(1))
        for linha in linhas
        if (m := padrao_titulo.search(linha["titulo"]))
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


# --- Montagem de item a partir de uma linha da CSV --------------------

_PADRAO_ACORDAO_TAG = re.compile(
    r'<acordao_decisao_tcu colegiado="([^"]*)" numero="([^"]*)" ano="([^"]*)"\s*>'
    r"(.*?)</acordao_decisao_tcu>"
)
_PADRAO_TAG_TCU = re.compile(r"<[a-z_]+_tcu[^>]*>(.*?)</[a-z_]+_tcu>")
_PADRAO_EDICAO_NUMERO = re.compile(r"(\d+)/(\d+)\s*$")


def _montar_item(linha: dict, titulo_prefixo: str) -> ItemColetado | None:
    texto_acordao_bruto = linha.get("TEXTOACORDAO", "")
    m_acordao = _PADRAO_ACORDAO_TAG.search(texto_acordao_bruto)
    if not m_acordao:
        return None

    m_edicao = _PADRAO_EDICAO_NUMERO.search(linha.get("TITULO", ""))
    if not m_edicao:
        return None  # sem número de edição no título, não deveria chegar aqui

    colegiado, numero_acordao, ano_acordao, _ = m_acordao.groups()
    citacao = _PADRAO_TAG_TCU.sub(r"\1", texto_acordao_bruto)
    numero_edicao, ano_edicao = m_edicao.groups()

    url_origem = _url_busca_acordao(colegiado, numero_acordao, ano_acordao)

    enunciado = linha.get("ENUNCIADO", "").strip()
    titulo = (
        f"{titulo_prefixo} {numero_edicao}/{ano_edicao} — "
        f"Acórdão {numero_acordao}/{ano_acordao} {colegiado}"
    )

    partes = [titulo, "", f"Citação: {citacao}", "", f"Ementa: {enunciado}"]
    texto_info = linha.get("TEXTOINFO", "").strip()
    if texto_info:
        partes.append(f"\nDetalhamento:\n{texto_info}")

    return ItemColetado(
        url_origem=url_origem,
        titulo=titulo,
        data_publicacao=None,
        texto_bruto="\n".join(partes).strip(),
    )


def _url_busca_acordao(colegiado: str, numero: str, ano: str) -> str:
    consulta = f'COLEGIADO:"{colegiado}" NUMACORDAO:{numero} ANOACORDAO:{ano}'
    return BUSCA_ACORDAO_URL_BASE.format(query=quote(consulta, safe=""))
