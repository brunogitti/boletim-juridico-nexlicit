"""Camada de persistência: schema, conexão, transação e acesso ao SQLite.

Todas as funções de escrita (`inserir_*`, `marcar_*`, `abrir_execucao`,
`fechar_execucao`, `seed_fontes`) executam mas não commitam sozinhas — quem
chama envolve com `with transacao(conexao):`. Isso mantém "toda escrita
dentro de transação" como decisão de quem orquestra, não de cada função.
"""

import contextlib
import json
import sqlite3
from datetime import datetime, timezone

_DDL = """
CREATE TABLE IF NOT EXISTS fontes (
    id            INTEGER PRIMARY KEY,
    nome          TEXT NOT NULL UNIQUE,
    tipo          TEXT NOT NULL,        -- 'tribunal' | 'especializada'
    url_base      TEXT NOT NULL,
    prioridade    INTEGER NOT NULL,     -- ordem no boletim
    ativo         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS itens_brutos (
    id                INTEGER PRIMARY KEY,
    fonte_id          INTEGER NOT NULL REFERENCES fontes(id),
    url_origem        TEXT NOT NULL,
    titulo            TEXT,
    data_publicacao   TEXT,             -- ISO 8601
    texto_bruto       TEXT NOT NULL,
    hash_conteudo     TEXT NOT NULL,
    coletado_em       TEXT NOT NULL,
    status            TEXT NOT NULL     -- 'coletado'|'fatiado'|'erro'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_itens_hash ON itens_brutos(hash_conteudo);

CREATE TABLE IF NOT EXISTS decisoes (
    id                 INTEGER PRIMARY KEY,
    item_bruto_id      INTEGER NOT NULL REFERENCES itens_brutos(id),
    chave_dedup        TEXT NOT NULL,
    tribunal           TEXT NOT NULL,
    numero_acordao     TEXT,
    numero_processo    TEXT,
    orgao_julgador     TEXT,
    relator            TEXT,
    data_julgamento    TEXT,
    url_inteiro_teor   TEXT,
    tema               TEXT,
    artigos_lei        TEXT,            -- JSON array
    impacto            TEXT,            -- 'alto'|'medio'|'baixo'
    resumo             TEXT,
    triagem_status     TEXT NOT NULL,   -- 'relevante'|'descartado'|'sem_ancora'
    triagem_motivo     TEXT,
    analisado_em       TEXT,
    enviado_em         TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_decisoes_dedup ON decisoes(chave_dedup);
CREATE INDEX IF NOT EXISTS idx_decisoes_envio ON decisoes(enviado_em, impacto);

CREATE TABLE IF NOT EXISTS execucoes (
    id                INTEGER PRIMARY KEY,
    iniciado_em       TEXT NOT NULL,
    finalizado_em     TEXT,
    itens_coletados   INTEGER DEFAULT 0,
    itens_triados     INTEGER DEFAULT 0,
    decisoes_enviadas INTEGER DEFAULT 0,
    email_enviado     INTEGER DEFAULT 0,
    erros             TEXT              -- JSON array
);
"""

# (prioridade, nome, tipo, url_base) — ordem da Camada 6 de ARQUITETURA.md:
# "TCU, TCE-SP, STJ, TCE-MG, TCE-PR, demais". url_base de STJ e TCE-MG é o
# domínio raiz do tribunal: a página exata do informativo ainda é pendência
# de descoberta das Etapas 3d e 3b.
_FONTES_SEED = [
    (1, "TCU — Informativo de Licitações e Contratos", "tribunal",
     "https://portal.tcu.gov.br/jurisprudencia"),
    (2, "TCU — Boletim de Jurisprudência", "tribunal",
     "https://portal.tcu.gov.br/jurisprudencia"),
    (3, "TCE-SP — Boletim de Jurisprudência + Súmulas", "tribunal",
     "https://www.tce.sp.gov.br/boletim-de-jurisprudencia"),
    (4, "STJ — Informativo de Jurisprudência", "tribunal",
     "https://www.stj.jus.br"),
    (5, "TCE-MG — Informativo de Jurisprudência", "tribunal",
     "https://www.tce.mg.gov.br"),
    (6, "TCE-PR — Boletim Informativo de Jurisprudência", "tribunal",
     "https://www.tce.pr.gov.br/fiscalizado/informativos-do-tcepr/boletim-informativo-de-jurisprudencia/"),
    (7, "Zênite", "especializada", "https://zenite.com.br/noticias/"),
]


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def conectar(caminho_banco: str) -> sqlite3.Connection:
    """Abre a conexão com row_factory=Row e foreign_keys ligado.

    Sem a pragma, o SQLite ignora as REFERENCES silenciosamente.
    """
    conexao = sqlite3.connect(caminho_banco)
    conexao.row_factory = sqlite3.Row
    conexao.execute("PRAGMA foreign_keys = ON")
    return conexao


def criar_schema(conexao: sqlite3.Connection) -> None:
    """Cria as tabelas/índices se não existirem.

    Idempotente: seguro rodar a cada execução do job, não só na primeira.
    """
    conexao.executescript(_DDL)


@contextlib.contextmanager
def transacao(conexao: sqlite3.Connection):
    """Agrupa escritas em uma transação: commita no fim do bloco, ou desfaz
    tudo se algo dentro do bloco levantar exceção."""
    try:
        yield conexao
        conexao.commit()
    except Exception:
        conexao.rollback()
        raise


def seed_fontes(conexao: sqlite3.Connection) -> None:
    """Insere as sete fontes de ARQUITETURA.md §2, na ordem de prioridade
    do boletim. Idempotente: nome já existente não gera duplicata nem erro.
    """
    conexao.executemany(
        """
        INSERT INTO fontes (prioridade, nome, tipo, url_base)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(nome) DO NOTHING
        """,
        _FONTES_SEED,
    )


def inserir_item_bruto(conexao: sqlite3.Connection, *, fonte_id: int,
                        url_origem: str, titulo: str | None,
                        data_publicacao: str | None, texto_bruto: str,
                        hash_conteudo: str,
                        status: str = "coletado") -> int | None:
    """Insere em itens_brutos.

    Devolve o id novo, ou None se hash_conteudo já existia (mesmo conteúdo
    já coletado antes — não é erro, é o índice único fazendo o trabalho).
    """
    cursor = conexao.execute(
        """
        INSERT INTO itens_brutos
            (fonte_id, url_origem, titulo, data_publicacao, texto_bruto,
             hash_conteudo, coletado_em, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hash_conteudo) DO NOTHING
        """,
        (fonte_id, url_origem, titulo, data_publicacao, texto_bruto,
         hash_conteudo, _agora_iso(), status),
    )
    return cursor.lastrowid if cursor.rowcount else None


def inserir_decisao(conexao: sqlite3.Connection, *, item_bruto_id: int,
                     chave_dedup: str, tribunal: str, triagem_status: str,
                     numero_acordao: str | None = None,
                     numero_processo: str | None = None,
                     orgao_julgador: str | None = None,
                     relator: str | None = None,
                     data_julgamento: str | None = None,
                     url_inteiro_teor: str | None = None,
                     tema: str | None = None,
                     artigos_lei: list[str] | None = None,
                     impacto: str | None = None,
                     resumo: str | None = None,
                     triagem_motivo: str | None = None) -> int | None:
    """Insere em decisoes.

    Devolve o id novo, ou None se chave_dedup já existia (ON CONFLICT DO
    NOTHING) — é isso que impede decisão repetida no boletim.
    """
    cursor = conexao.execute(
        """
        INSERT INTO decisoes (
            item_bruto_id, chave_dedup, tribunal, numero_acordao,
            numero_processo, orgao_julgador, relator, data_julgamento,
            url_inteiro_teor, tema, artigos_lei, impacto, resumo,
            triagem_status, triagem_motivo
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chave_dedup) DO NOTHING
        """,
        (item_bruto_id, chave_dedup, tribunal, numero_acordao,
         numero_processo, orgao_julgador, relator, data_julgamento,
         url_inteiro_teor, tema,
         json.dumps(artigos_lei) if artigos_lei is not None else None,
         impacto, resumo, triagem_status, triagem_motivo),
    )
    return cursor.lastrowid if cursor.rowcount else None


def marcar_decisao_enviada(conexao: sqlite3.Connection,
                            decisao_id: int) -> None:
    """Seta enviado_em = agora (UTC ISO 8601)."""
    conexao.execute(
        "UPDATE decisoes SET enviado_em = ? WHERE id = ?",
        (_agora_iso(), decisao_id),
    )


_NAO_INFORMADO = object()  # sentinela: distingue "não passou o argumento"
# de "passou None de propósito" — usado só por numero_acordao/
# numero_processo abaixo, os únicos campos que não podem ser apagados por
# engano quando o chamador simplesmente não tem nada novo pra dizer


def atualizar_analise(conexao: sqlite3.Connection, decisao_id: int, *,
                       artigos_lei: list[str] | None = None,
                       impacto: str | None = None,
                       resumo: str | None = None,
                       triagem_status: str | None = None,
                       numero_acordao=_NAO_INFORMADO,
                       numero_processo=_NAO_INFORMADO) -> None:
    """Grava o resultado da Camada 5 (Etapa 6) numa decisão que já existe
    (criada pela triagem, Etapa 5). Sempre seta analisado_em = agora,
    mesmo quando triagem_status vira 'sem_ancora' sem passar pelo LLM —
    'analisado' aqui quer dizer "a Camada 5 já processou este item", não
    "o LLM foi chamado". triagem_status só é alterado quando informado
    (o caminho normal mantém 'relevante'; sem_ancora troca).

    numero_acordao/numero_processo são diferentes: None aqui é um valor
    válido pros outros campos (limpa o campo), mas pra estes dois um
    "esqueci de passar" não pode virar um UPDATE que apaga um número já
    correto — por isso só entram no SET quando o chamador passa
    explicitamente (mesmo que seja None), via sentinela `_NAO_INFORMADO`
    como padrão. Uso real: nucleo/analise.py recupera esses dois do
    rodapé "Serviço" do texto completo quando a triagem devolveu None por
    causa do teto de 1200 caracteres do trecho — só grava quando achou
    algo, nunca sobrescreve com None."""
    campos = ["analisado_em = ?"]
    valores: list = [_agora_iso()]

    campos.append("artigos_lei = ?")
    valores.append(json.dumps(artigos_lei) if artigos_lei is not None else None)
    campos.append("impacto = ?")
    valores.append(impacto)
    campos.append("resumo = ?")
    valores.append(resumo)
    if triagem_status is not None:
        campos.append("triagem_status = ?")
        valores.append(triagem_status)
    if numero_acordao is not _NAO_INFORMADO:
        campos.append("numero_acordao = ?")
        valores.append(numero_acordao)
    if numero_processo is not _NAO_INFORMADO:
        campos.append("numero_processo = ?")
        valores.append(numero_processo)

    valores.append(decisao_id)
    conexao.execute(
        f"UPDATE decisoes SET {', '.join(campos)} WHERE id = ?", valores,
    )


def abrir_execucao(conexao: sqlite3.Connection) -> int:
    """Insere uma linha em execucoes com iniciado_em = agora. Devolve o id."""
    cursor = conexao.execute(
        "INSERT INTO execucoes (iniciado_em) VALUES (?)",
        (_agora_iso(),),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def fechar_execucao(conexao: sqlite3.Connection, execucao_id: int, *,
                     itens_coletados: int = 0, itens_triados: int = 0,
                     decisoes_enviadas: int = 0,
                     email_enviado: bool = False,
                     erros: list[str] | None = None) -> None:
    """Seta finalizado_em e os contadores finais da execução."""
    conexao.execute(
        """
        UPDATE execucoes
        SET finalizado_em = ?, itens_coletados = ?, itens_triados = ?,
            decisoes_enviadas = ?, email_enviado = ?, erros = ?
        WHERE id = ?
        """,
        (_agora_iso(), itens_coletados, itens_triados, decisoes_enviadas,
         int(email_enviado), json.dumps(erros or []), execucao_id),
    )
