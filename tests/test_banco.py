import pytest

from nucleo.banco import (
    _FONTES_SEED,
    atualizar_analise,
    conectar,
    criar_schema,
    inserir_decisao,
    inserir_item_bruto,
    seed_fontes,
    transacao,
)


@pytest.fixture
def conexao():
    conexao = conectar(":memory:")
    criar_schema(conexao)
    yield conexao
    conexao.close()


def _inserir_fonte_teste(conexao) -> int:
    cursor = conexao.execute(
        "INSERT INTO fontes (nome, tipo, url_base, prioridade) VALUES (?, ?, ?, ?)",
        ("Fonte de teste", "tribunal", "https://exemplo.gov.br", 1),
    )
    conexao.commit()
    return cursor.lastrowid


def _inserir_item_bruto_teste(conexao, fonte_id: int) -> int:
    item_id = inserir_item_bruto(
        conexao,
        fonte_id=fonte_id,
        url_origem="https://exemplo.gov.br/item",
        titulo="Item de teste",
        data_publicacao="2026-07-30",
        texto_bruto="texto",
        hash_conteudo="hash-unico-de-teste",
    )
    conexao.commit()
    assert item_id is not None
    return item_id


def test_criar_schema_cria_tabelas(conexao):
    linhas = conexao.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
    ).fetchall()
    nomes = {linha["name"] for linha in linhas}

    assert {"fontes", "itens_brutos", "decisoes", "execucoes"} <= nomes
    assert {"idx_itens_hash", "idx_decisoes_dedup", "idx_decisoes_envio"} <= nomes


def test_criar_schema_e_idempotente(conexao):
    criar_schema(conexao)
    criar_schema(conexao)

    _inserir_fonte_teste(conexao)
    total = conexao.execute("SELECT COUNT(*) FROM fontes").fetchone()[0]
    assert total == 1


def test_transacao_desfaz_tudo_em_erro(conexao):
    with pytest.raises(RuntimeError):
        with transacao(conexao):
            conexao.execute(
                "INSERT INTO fontes (nome, tipo, url_base, prioridade) VALUES (?, ?, ?, ?)",
                ("Fonte que não deve ficar", "tribunal", "https://exemplo.gov.br", 1),
            )
            raise RuntimeError("erro proposital dentro da transação")

    total = conexao.execute("SELECT COUNT(*) FROM fontes").fetchone()[0]
    assert total == 0


def test_indice_unico_de_dedup(conexao):
    fonte_id = _inserir_fonte_teste(conexao)
    item_id = _inserir_item_bruto_teste(conexao, fonte_id)

    primeiro_id = inserir_decisao(
        conexao,
        item_bruto_id=item_id,
        chave_dedup="tce-pr|3190/2025|2025",
        tribunal="TCE-PR",
        triagem_status="relevante",
    )
    conexao.commit()

    segundo_id = inserir_decisao(
        conexao,
        item_bruto_id=item_id,
        chave_dedup="tce-pr|3190/2025|2025",
        tribunal="TCE-PR",
        triagem_status="relevante",
    )
    conexao.commit()

    assert primeiro_id is not None
    assert segundo_id is None

    total = conexao.execute("SELECT COUNT(*) FROM decisoes").fetchone()[0]
    assert total == 1


def test_seed_fontes_insere_sete_e_e_idempotente(conexao):
    seed_fontes(conexao)
    seed_fontes(conexao)
    conexao.commit()

    total = conexao.execute("SELECT COUNT(*) FROM fontes").fetchone()[0]
    assert total == len(_FONTES_SEED) == 7

    prioridades = [
        linha["prioridade"]
        for linha in conexao.execute(
            "SELECT prioridade FROM fontes ORDER BY prioridade"
        ).fetchall()
    ]
    assert prioridades == [1, 2, 3, 4, 5, 6, 7]


def _inserir_decisao_teste(conexao, item_id: int, **overrides) -> int:
    padrao = dict(
        item_bruto_id=item_id, chave_dedup="chave-unica-de-teste",
        tribunal="Zênite", triagem_status="relevante",
    )
    padrao.update(overrides)
    decisao_id = inserir_decisao(conexao, **padrao)
    conexao.commit()
    assert decisao_id is not None
    return decisao_id


def test_atualizar_analise_grava_resumo_artigos_impacto(conexao):
    fonte_id = _inserir_fonte_teste(conexao)
    item_id = _inserir_item_bruto_teste(conexao, fonte_id)
    decisao_id = _inserir_decisao_teste(conexao, item_id)

    atualizar_analise(
        conexao, decisao_id,
        artigos_lei=["art. 67"], impacto="alto", resumo="Resumo de teste.",
    )
    conexao.commit()

    linha = conexao.execute("SELECT * FROM decisoes WHERE id = ?", (decisao_id,)).fetchone()
    assert linha["impacto"] == "alto"
    assert linha["resumo"] == "Resumo de teste."
    assert linha["analisado_em"] is not None


def test_atualizar_analise_sem_numero_nao_apaga_valor_existente(conexao):
    # achado real (2026-08-11): numero_acordao/numero_processo não podem
    # ser tratados como os outros campos — "não informei" tem que deixar
    # o valor como está, nunca virar UPDATE ... = NULL por omissão
    fonte_id = _inserir_fonte_teste(conexao)
    item_id = _inserir_item_bruto_teste(conexao, fonte_id)
    decisao_id = _inserir_decisao_teste(conexao, item_id, numero_acordao="123/2026")

    atualizar_analise(conexao, decisao_id, resumo="Resumo sem tocar no número.")
    conexao.commit()

    linha = conexao.execute("SELECT numero_acordao FROM decisoes WHERE id = ?", (decisao_id,)).fetchone()
    assert linha["numero_acordao"] == "123/2026"


def test_atualizar_analise_com_numero_explicito_atualiza(conexao):
    fonte_id = _inserir_fonte_teste(conexao)
    item_id = _inserir_item_bruto_teste(conexao, fonte_id)
    decisao_id = _inserir_decisao_teste(conexao, item_id)  # numero_acordao None

    atualizar_analise(
        conexao, decisao_id, numero_acordao="1230/2026", numero_processo="300136/26",
    )
    conexao.commit()

    linha = conexao.execute(
        "SELECT numero_acordao, numero_processo FROM decisoes WHERE id = ?", (decisao_id,),
    ).fetchone()
    assert linha["numero_acordao"] == "1230/2026"
    assert linha["numero_processo"] == "300136/26"
