from pathlib import Path

import pytest
import requests

import coletores.tce_mg as tce_mg
from nucleo.banco import conectar, criar_schema, inserir_item_bruto, seed_fontes

FIXTURES = Path(__file__).parent / "fixtures" / "tce_mg"


class _RespostaFalsa:
    def __init__(self, texto: str):
        self.text = texto


@pytest.fixture
def conexao():
    conexao = conectar(":memory:")
    criar_schema(conexao)
    seed_fontes(conexao)
    conexao.commit()
    yield conexao
    conexao.close()


def _fonte_id_tce_mg(conexao) -> int:
    linha = conexao.execute(
        "SELECT id FROM fontes WHERE nome LIKE 'TCE-MG%'"
    ).fetchone()
    assert linha is not None
    return linha["id"]


def test_padrao_link_informativo_usa_fixture_real():
    pagina1 = (FIXTURES / "indice_pagina1.html").read_text(encoding="utf-8")

    itens = list(tce_mg._PADRAO_LINK_INFORMATIVO.finditer(pagina1))

    assert len(itens) == 5
    dia, mes, ano, href, titulo = itens[0].groups()
    assert (dia, mes, ano) == ("21", "07", "2026")
    assert href == "/Informativo-de-Jurisprudencia-n-334.html/Noticia/1111629019"
    assert titulo == "Informativo de Jurisprudência n. 334"

    # mesma URL do exemplo original dado na etapa
    _, _, _, href_333, _ = itens[1].groups()
    assert href_333 == "/Informativo-de-Jurisprudencia-n-333.html/Noticia/1111628977"


def test_listar_pendentes_pagina_toda_quando_nada_coletado(monkeypatch):
    pagina1 = (FIXTURES / "indice_pagina1.html").read_text(encoding="utf-8")
    pagina2 = (FIXTURES / "indice_pagina2.html").read_text(encoding="utf-8")

    def _requisitar_falso(sessao, url):
        return _RespostaFalsa(pagina2 if "paginacao=2" in url else pagina1)

    monkeypatch.setattr(tce_mg, "_requisitar", _requisitar_falso)

    pendentes = tce_mg._listar_informativos_pendentes(
        requests.Session(), ja_coletados=set(), max_paginas=2
    )

    assert len(pendentes) == 7  # 5 da página 1 + 2 da página 2
    assert pendentes[0].numero == "334"
    assert pendentes[0].url == (
        "https://www.tce.mg.gov.br/Informativo-de-Jurisprudencia-n-334.html/Noticia/1111629019"
    )
    assert pendentes[0].data_publicacao == "2026-07-21T03:00:00+00:00"
    assert pendentes[-1].numero == "314"


def test_listar_pendentes_para_de_paginar_ao_achar_edicao_conhecida(monkeypatch):
    pagina1 = (FIXTURES / "indice_pagina1.html").read_text(encoding="utf-8")
    pagina2 = (FIXTURES / "indice_pagina2.html").read_text(encoding="utf-8")
    chamadas = []

    def _requisitar_falso(sessao, url):
        chamadas.append(url)
        return _RespostaFalsa(pagina2 if "paginacao=2" in url else pagina1)

    monkeypatch.setattr(tce_mg, "_requisitar", _requisitar_falso)

    # 332, 331 e 330 (os 3 últimos da página 1) já foram coletados antes
    ja_coletados = {
        "https://www.tce.mg.gov.br/Informativo-de-Jurisprudencia-n-332.html/Noticia/1111628944",
        "https://www.tce.mg.gov.br/Informativo-de-Jurisprudencia-n-331.html/Noticia/1111628905",
        "https://www.tce.mg.gov.br/Informativo-de-Jurisprudencia-n-330.html/Noticia/1111628863",
    }

    pendentes = tce_mg._listar_informativos_pendentes(
        requests.Session(), ja_coletados=ja_coletados, max_paginas=5
    )

    assert [p.numero for p in pendentes] == ["334", "333"]
    # a página 2 nunca devia ter sido buscada
    assert all("paginacao=2" not in url for url in chamadas)
    assert len(chamadas) == 1


def test_extrair_conteudo_principal_preserva_os_dois_padroes_de_citacao():
    html_texto = (FIXTURES / "informativo_333.html").read_text(encoding="utf-8")

    conteudo = tce_mg._extrair_conteudo_principal(html_texto)

    assert "<style" not in conteudo
    assert "display: none" not in conteudo

    # citação do próprio TCE-MG: tem link, não tem número de acórdão
    assert (
        'Processo <a href="https://tcjuris.tce.mg.gov.br/Home/Detalhes/1152957">'
        "1152957</a> - Representação" in conteudo
    )
    assert "Acórdão" not in conteudo.split("Tribunal de Contas da União")[0]

    # citação do TCU dentro do boletim: tem número de acórdão, mas sem link
    assert "<b>Acórdão 1370/2026 Plenário</b>" in conteudo
    assert '<a href="https://viajuris' not in conteudo  # não é citação do TCE-PR


def test_coletar_grava_apenas_informativos_novos(conexao, monkeypatch):
    fonte_id = _fonte_id_tce_mg(conexao)

    pendentes = [
        tce_mg.InformativoIndice(
            numero="334", url="https://www.tce.mg.gov.br/x334.htm",
            titulo="Informativo 334", data_publicacao="2026-07-21T03:00:00+00:00",
        ),
    ]
    monkeypatch.setattr(
        tce_mg, "_listar_informativos_pendentes",
        lambda sessao, ja_coletados, **kw: pendentes,
    )

    def _coletar_informativo_falso(sessao, informativo):
        return tce_mg.ItemColetado(
            url_origem=informativo.url, titulo=informativo.titulo,
            data_publicacao=informativo.data_publicacao, texto_bruto="texto de teste",
        )

    monkeypatch.setattr(tce_mg, "_coletar_informativo", _coletar_informativo_falso)

    resultado = tce_mg.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 1
    assert resultado.itens_repetidos == 0
    assert resultado.erro is None


def test_coletar_falha_isolada_por_edicao(conexao, monkeypatch):
    fonte_id = _fonte_id_tce_mg(conexao)

    pendentes = [
        tce_mg.InformativoIndice(numero="2", url="https://x/2.htm", titulo="B2",
                                  data_publicacao="2026-01-01T00:00:00+00:00"),
        tce_mg.InformativoIndice(numero="1", url="https://x/1.htm", titulo="B1",
                                  data_publicacao="2026-01-01T00:00:00+00:00"),
    ]
    monkeypatch.setattr(
        tce_mg, "_listar_informativos_pendentes",
        lambda sessao, ja_coletados, **kw: pendentes,
    )

    def _coletar_informativo_falso(sessao, informativo):
        if informativo.numero == "2":
            raise RuntimeError("página quebrada")
        return tce_mg.ItemColetado(
            url_origem=informativo.url, titulo=informativo.titulo,
            data_publicacao=informativo.data_publicacao, texto_bruto="texto ok",
        )

    monkeypatch.setattr(tce_mg, "_coletar_informativo", _coletar_informativo_falso)

    resultado = tce_mg.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 1
    assert resultado.erro is None


def test_coletar_falha_isolada_quando_tudo_falha(conexao, monkeypatch):
    fonte_id = _fonte_id_tce_mg(conexao)
    monkeypatch.setattr(
        tce_mg, "_listar_informativos_pendentes",
        lambda sessao, ja_coletados, **kw: (_ for _ in ()).throw(RuntimeError("índice fora do ar")),
    )

    resultado = tce_mg.coletar(conexao, fonte_id)

    assert resultado.itens_novos == 0
    assert resultado.erro is not None


def test_informativos_ja_coletados(conexao):
    fonte_id = _fonte_id_tce_mg(conexao)
    inserir_item_bruto(
        conexao, fonte_id=fonte_id, url_origem="https://www.tce.mg.gov.br/x.htm",
        titulo="X", data_publicacao="2026-01-01T00:00:00+00:00", texto_bruto="texto",
        hash_conteudo="hash-de-teste-x",
    )
    conexao.commit()

    assert tce_mg._informativos_ja_coletados(conexao, fonte_id) == {
        "https://www.tce.mg.gov.br/x.htm"
    }
