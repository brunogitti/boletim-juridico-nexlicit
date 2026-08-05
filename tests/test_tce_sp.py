from pathlib import Path

import pytest
import requests

import coletores.tce_sp as tce_sp
from nucleo.banco import conectar, criar_schema, inserir_item_bruto, seed_fontes

FIXTURES = Path(__file__).parent / "fixtures" / "tce_sp"


class _RespostaFalsa:
    def __init__(self, *, texto=None, conteudo=None):
        self.text = texto
        self.content = conteudo


@pytest.fixture
def conexao():
    conexao = conectar(":memory:")
    criar_schema(conexao)
    seed_fontes(conexao)
    conexao.commit()
    yield conexao
    conexao.close()


def _fonte_id_tce_sp(conexao) -> int:
    linha = conexao.execute(
        "SELECT id FROM fontes WHERE nome LIKE 'TCE-SP%'"
    ).fetchone()
    assert linha is not None
    return linha["id"]


# --- Súmulas -----------------------------------------------------------

def test_coletar_sumulas_usa_fixture_real(monkeypatch):
    pagina = (FIXTURES / "sumulas.html").read_text(encoding="utf-8")
    monkeypatch.setattr(
        tce_sp, "_requisitar",
        lambda sessao, url: _RespostaFalsa(texto=pagina),
    )

    itens = tce_sp._coletar_sumulas(sessao=None)

    assert len(itens) == 2
    normal, cancelada = itens

    assert normal.titulo == "Súmula TCE-SP n.º 1"
    assert normal.url_origem == (
        "https://www.tce.sp.gov.br/boletim-de-jurisprudencia/sumulas#sum-modal-1"
    )
    assert "(Veja histórico e fundamento)" not in normal.texto_bruto.split(
        "HISTÓRICO E FUNDAMENTO"
    )[0]
    assert "Alterada pela Resolução nº 06/1991" in normal.texto_bruto
    # última data do histórico (a alteração, não a aprovação)
    assert normal.data_publicacao == "1991-06-18T03:00:00+00:00"

    assert cancelada.titulo == "Súmula TCE-SP n.º 5 (CANCELADA)"
    assert "CANCELADA" in cancelada.texto_bruto
    assert cancelada.data_publicacao == "2016-12-15T03:00:00+00:00"


def test_ultima_data_doe_pega_a_mais_recente():
    texto = (
        "Aprovada pela Resolução nº 1/2000 (DOE de 01/01/2000)\n"
        "Alterada pela Resolução nº 2/2010 (DOE de 15/06/2010)"
    )
    assert tce_sp._ultima_data_doe(texto) == "2010-06-15T03:00:00+00:00"


def test_ultima_data_doe_sem_match_devolve_none():
    assert tce_sp._ultima_data_doe("nada de data aqui") is None


# --- Boletim: listagem paginada -----------------------------------------

def test_padrao_publicacao_usa_fixture_real():
    pagina0 = (FIXTURES / "publicacoes_pagina0.html").read_text(encoding="utf-8")

    itens = tce_sp._PADRAO_PUBLICACAO.findall(pagina0)

    assert len(itens) == 5
    href, titulo = itens[0]
    assert href == (
        "https://www.tce.sp.gov.br/boletim-de-jurisprudencia/publicacoes/"
        "boletim-jurisprudencia-edicao-53-marco2026"
    )
    assert "Edição N.º 53" in titulo
    # padrão de URL antigo (sem o prefixo boletim-de-jurisprudencia/)
    href_antigo, _ = itens[2]
    assert href_antigo == (
        "https://www.tce.sp.gov.br/publicacoes/"
        "boletim-jurisprudencia-edicao-51-novembro-e-dezembro2025"
    )


def test_listar_pendentes_pagina_toda_quando_nada_coletado(monkeypatch):
    pagina0 = (FIXTURES / "publicacoes_pagina0.html").read_text(encoding="utf-8")
    pagina1 = (FIXTURES / "publicacoes_pagina1.html").read_text(encoding="utf-8")

    def _requisitar_falso(sessao, url):
        return _RespostaFalsa(texto=pagina1 if "page=1" in url else pagina0)

    monkeypatch.setattr(tce_sp, "_requisitar", _requisitar_falso)

    pendentes = tce_sp._listar_edicoes_pendentes(
        requests.Session(), ja_coletadas=set(), max_paginas=2
    )

    assert len(pendentes) == 7  # 5 da página 0 + 2 da página 1


def test_listar_pendentes_para_de_paginar_ao_achar_edicao_conhecida(monkeypatch):
    pagina0 = (FIXTURES / "publicacoes_pagina0.html").read_text(encoding="utf-8")
    pagina1 = (FIXTURES / "publicacoes_pagina1.html").read_text(encoding="utf-8")
    chamadas = []

    def _requisitar_falso(sessao, url):
        chamadas.append(url)
        return _RespostaFalsa(texto=pagina1 if "page=1" in url else pagina0)

    monkeypatch.setattr(tce_sp, "_requisitar", _requisitar_falso)

    ja_coletadas = {
        "https://www.tce.sp.gov.br/publicacoes/boletim-jurisprudencia-edicao-50-outubro2025",
        "https://www.tce.sp.gov.br/publicacoes/boletim-jurisprudencia-edicao-49-setembro2025",
    }
    pendentes = tce_sp._listar_edicoes_pendentes(
        requests.Session(), ja_coletadas=ja_coletadas, max_paginas=5
    )

    assert len(pendentes) == 3  # 53, 52 e 51 (os que faltavam na página 0)
    assert all("page=1" not in url for url in chamadas)


# --- Boletim: página de uma edição + PDF ---------------------------------

def test_coletar_edicao_boletim_usa_fixtures_reais(monkeypatch):
    edicao_html = (FIXTURES / "edicao_53.html").read_text(encoding="utf-8")
    pdf_bytes = (FIXTURES / "boletim_edicao_53_recorte.pdf").read_bytes()

    def _requisitar_falso(sessao, url):
        if url.endswith(".pdf"):
            return _RespostaFalsa(conteudo=pdf_bytes)
        return _RespostaFalsa(texto=edicao_html)

    monkeypatch.setattr(tce_sp, "_requisitar", _requisitar_falso)

    edicao = tce_sp.EdicaoIndice(
        url="https://www.tce.sp.gov.br/boletim-de-jurisprudencia/publicacoes/boletim-jurisprudencia-edicao-53-marco2026",
        titulo="Boletim de Jurisprudência - Edição N.º 53 - Março/2026",
    )
    item = tce_sp._coletar_edicao_boletim(sessao=None, edicao=edicao)

    assert item.data_publicacao == "2026-07-24T12:00:00+00:00"
    assert "BOLETIM DE JURISPRUDÊNCIA" in item.texto_bruto
    assert "jurisprudencia.tce.sp.gov.br" in item.texto_bruto
    assert "observatorio/ods" not in item.texto_bruto


def test_extrair_texto_pdf_filtra_links_de_ods():
    pdf_bytes = (FIXTURES / "boletim_edicao_53_recorte.pdf").read_bytes()

    texto = tce_sp._extrair_texto_pdf(pdf_bytes)

    # 5 links de inteiro teor real (jurisprudencia.tce.sp.gov.br) — nem os
    # links de ODS nem o do YouTube (presentes no PDF, mas não são
    # inteiro teor de decisão) devem ter sido incluídos pelo filtro de domínio
    assert texto.count("jurisprudencia.tce.sp.gov.br") == 5
    assert "observatorio/ods" not in texto


# --- coletar(): orquestração e falha isolada ------------------------------

def test_coletar_grava_sumulas_e_boletim(conexao, monkeypatch):
    fonte_id = _fonte_id_tce_sp(conexao)

    itens_sumulas = [tce_sp.ItemColetado(
        url_origem="https://x/sumulas#sum-modal-1", titulo="Súmula TCE-SP n.º 1",
        data_publicacao=None, texto_bruto="texto da súmula",
    )]
    edicoes = [tce_sp.EdicaoIndice(url="https://x/edicao-1", titulo="Edição 1")]

    monkeypatch.setattr(tce_sp, "_coletar_sumulas", lambda sessao: itens_sumulas)
    monkeypatch.setattr(tce_sp, "_listar_edicoes_pendentes",
                         lambda sessao, ja_coletadas, **kw: edicoes)
    monkeypatch.setattr(
        tce_sp, "_coletar_edicao_boletim",
        lambda sessao, edicao: tce_sp.ItemColetado(
            url_origem=edicao.url, titulo=edicao.titulo,
            data_publicacao=None, texto_bruto="texto do boletim",
        ),
    )

    resultado = tce_sp.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 2  # 1 súmula + 1 edição
    assert resultado.erro is None


def test_coletar_falha_isolada_por_edicao_do_boletim(conexao, monkeypatch):
    fonte_id = _fonte_id_tce_sp(conexao)

    monkeypatch.setattr(tce_sp, "_coletar_sumulas", lambda sessao: [])
    edicoes = [
        tce_sp.EdicaoIndice(url="https://x/2", titulo="B2"),
        tce_sp.EdicaoIndice(url="https://x/1", titulo="B1"),
    ]
    monkeypatch.setattr(tce_sp, "_listar_edicoes_pendentes",
                         lambda sessao, ja_coletadas, **kw: edicoes)

    def _coletar_edicao_falso(sessao, edicao):
        if edicao.url == "https://x/2":
            raise RuntimeError("PDF corrompido")
        return tce_sp.ItemColetado(
            url_origem=edicao.url, titulo=edicao.titulo,
            data_publicacao=None, texto_bruto="texto ok",
        )

    monkeypatch.setattr(tce_sp, "_coletar_edicao_boletim", _coletar_edicao_falso)

    resultado = tce_sp.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 1
    assert resultado.erro is None


def test_coletar_falha_isolada_quando_tudo_falha(conexao, monkeypatch):
    fonte_id = _fonte_id_tce_sp(conexao)

    def _falha_sumulas(sessao):
        raise RuntimeError("súmulas fora do ar")

    def _falha_indice(sessao, ja_coletadas, **kw):
        raise RuntimeError("índice fora do ar")

    monkeypatch.setattr(tce_sp, "_coletar_sumulas", _falha_sumulas)
    monkeypatch.setattr(tce_sp, "_listar_edicoes_pendentes", _falha_indice)

    resultado = tce_sp.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 0
    assert resultado.erro is not None


def test_edicoes_ja_coletadas(conexao):
    fonte_id = _fonte_id_tce_sp(conexao)
    inserir_item_bruto(
        conexao, fonte_id=fonte_id, url_origem="https://www.tce.sp.gov.br/x",
        titulo="X", data_publicacao=None, texto_bruto="texto",
        hash_conteudo="hash-de-teste-x",
    )
    conexao.commit()

    assert tce_sp._edicoes_ja_coletadas(conexao, fonte_id) == {
        "https://www.tce.sp.gov.br/x"
    }
