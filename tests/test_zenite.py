import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import coletores.zenite as zenite
from nucleo.banco import conectar, criar_schema, inserir_item_bruto, seed_fontes

FIXTURES = Path(__file__).parent / "fixtures" / "zenite"
DESDE_ANTIGO = datetime(2020, 1, 1, tzinfo=timezone.utc)


class _RespostaFalsa:
    """Substitui requests.Response nos testes: mesma interface mínima que
    o coletor usa (.text, .content, .json(), .headers), sem rede."""

    def __init__(self, *, texto=None, conteudo=None, dados_json=None, headers=None):
        self.text = texto
        self.content = (
            conteudo if conteudo is not None
            else (texto.encode("utf-8") if texto is not None else b"")
        )
        self._dados_json = dados_json
        self.headers = headers or {}

    def json(self):
        return self._dados_json


@pytest.fixture
def conexao():
    conexao = conectar(":memory:")
    criar_schema(conexao)
    seed_fontes(conexao)
    conexao.commit()
    yield conexao
    conexao.close()


def _fonte_id_zenite(conexao) -> int:
    linha = conexao.execute("SELECT id FROM fontes WHERE nome = 'Zênite'").fetchone()
    assert linha is not None
    return linha["id"]


def test_coletar_wp_json_usa_fixture_real(monkeypatch):
    posts = json.loads((FIXTURES / "wp_json_posts.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(
        zenite, "_requisitar",
        lambda sessao, url, params=None: _RespostaFalsa(
            dados_json=posts, headers={"X-WP-TotalPages": "1"}
        ),
    )

    itens = zenite._coletar_wp_json(sessao=None, desde=DESDE_ANTIGO)

    assert len(itens) == 2
    primeiro = itens[0]
    assert primeiro.titulo == (
        "TCE/SC suspende licitação para serviço de guincho por indícios "
        "de irregularidades"
    )
    assert primeiro.url_origem == (
        "https://zenite.com.br/2026/07/30/"
        "tce-sc-suspende-licitacao-para-servico-de-guincho-por-indicios-"
        "de-irregularidades/"
    )
    assert primeiro.data_publicacao == "2026-07-30T06:00:00+00:00"
    assert "Tribunal de Contas de Santa Catarina" in primeiro.texto_bruto
    assert "<p>" not in primeiro.texto_bruto
    assert "&atilde;" not in primeiro.texto_bruto


def test_coletar_rss_usa_fixture_real(monkeypatch):
    conteudo = (FIXTURES / "feed.xml").read_bytes()
    monkeypatch.setattr(
        zenite, "_requisitar",
        lambda sessao, url, params=None: _RespostaFalsa(conteudo=conteudo),
    )

    itens = zenite._coletar_rss(sessao=None, desde=DESDE_ANTIGO)

    assert len(itens) == 2
    assert itens[0].data_publicacao == "2026-07-30T06:00:00+00:00"
    assert itens[1].data_publicacao == "2026-07-29T06:00:00+00:00"
    assert "Tribunal de Contas de Santa Catarina" in itens[0].texto_bruto


def test_coletar_rss_filtra_por_desde(monkeypatch):
    conteudo = (FIXTURES / "feed.xml").read_bytes()
    monkeypatch.setattr(
        zenite, "_requisitar",
        lambda sessao, url, params=None: _RespostaFalsa(conteudo=conteudo),
    )

    desde = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)  # entre os 2 itens
    itens = zenite._coletar_rss(sessao=None, desde=desde)

    assert len(itens) == 1
    assert itens[0].data_publicacao == "2026-07-30T06:00:00+00:00"


def test_coletar_html_usa_fixtures_reais(monkeypatch):
    listagem = (FIXTURES / "noticias.html").read_text(encoding="utf-8")
    artigo = (FIXTURES / "artigo_exemplo.html").read_text(encoding="utf-8")

    def _requisitar_falso(sessao, url, params=None):
        if url.endswith("/noticias/"):
            return _RespostaFalsa(texto=listagem)
        return _RespostaFalsa(texto=artigo)

    monkeypatch.setattr(zenite, "_requisitar", _requisitar_falso)

    itens = zenite._coletar_html(sessao=None, desde=DESDE_ANTIGO)

    assert len(itens) == 3
    primeiro = itens[0]
    assert primeiro.titulo == (
        "TCE/SC suspende licitação para serviço de guincho por indícios "
        "de irregularidades"
    )
    assert primeiro.url_origem == (
        "https://zenite.com.br/2026/07/30/"
        "tce-sc-suspende-licitacao-para-servico-de-guincho-por-indicios-"
        "de-irregularidades/"
    )
    # data sem hora ("30 de julho de 2026") vira meia-noite de Brasília em UTC
    assert primeiro.data_publicacao == "2026-07-30T03:00:00+00:00"
    assert "Tribunal de Contas de Santa Catarina" in primeiro.texto_bruto


def test_coletar_cai_para_rss_quando_wp_json_falha(conexao, monkeypatch):
    fonte_id = _fonte_id_zenite(conexao)

    def _wp_json_com_falha(sessao, desde):
        raise RuntimeError("wp-json fora do ar")

    itens_rss = [zenite.ItemColetado(
        url_origem="https://zenite.com.br/exemplo/",
        titulo="Item via RSS",
        data_publicacao="2026-07-30T06:00:00+00:00",
        texto_bruto="texto de teste",
    )]

    monkeypatch.setattr(zenite, "_coletar_wp_json", _wp_json_com_falha)
    monkeypatch.setattr(zenite, "_coletar_rss", lambda sessao, desde: itens_rss)

    resultado = zenite.coletar(conexao, fonte_id)

    assert resultado.origem == "rss"
    assert resultado.itens_novos == 1
    assert resultado.itens_repetidos == 0
    assert resultado.erro is None


def test_coletar_falha_isolada_quando_tudo_falha(conexao, monkeypatch):
    fonte_id = _fonte_id_zenite(conexao)

    def _falha(sessao, desde):
        raise RuntimeError("fora do ar")

    monkeypatch.setattr(zenite, "_coletar_wp_json", _falha)
    monkeypatch.setattr(zenite, "_coletar_rss", _falha)
    monkeypatch.setattr(zenite, "_coletar_html", _falha)

    resultado = zenite.coletar(conexao, fonte_id)

    assert resultado.origem == "falha"
    assert resultado.itens_novos == 0
    assert resultado.erro is not None


def test_determinar_desde_usa_bootstrap_quando_fonte_vazia(conexao):
    fonte_id = _fonte_id_zenite(conexao)

    desde = zenite._determinar_desde(conexao, fonte_id, dias_bootstrap=7)

    agora = datetime.now(timezone.utc)
    assert agora - timedelta(days=7, minutes=1) < desde < agora - timedelta(days=6, hours=23)


def test_determinar_desde_usa_ultima_data_coletada(conexao):
    fonte_id = _fonte_id_zenite(conexao)
    inserir_item_bruto(
        conexao, fonte_id=fonte_id, url_origem="https://zenite.com.br/x/",
        titulo="X", data_publicacao="2026-07-15T10:00:00+00:00",
        texto_bruto="texto", hash_conteudo="hash-de-teste-x",
    )
    conexao.commit()

    desde = zenite._determinar_desde(conexao, fonte_id, dias_bootstrap=7)

    assert desde == datetime.fromisoformat("2026-07-15T10:00:00+00:00")
