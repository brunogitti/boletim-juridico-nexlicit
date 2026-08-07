# Boletim Jurídico NexLicit

E-mail diário (7h, horário de Brasília) com decisões e entendimentos sobre a
Lei 14.133/2021, coletados de tribunais de contas, do STJ e de fonte
especializada, triados por relevância para o fornecedor que vende ao setor
público. Se não houver nada relevante no dia, não sai e-mail.

Arquitetura completa em [docs/ARQUITETURA.md](docs/ARQUITETURA.md). Roteiro
de sessões de desenvolvimento em [docs/PROMPTS-PLAN-MODE.md](docs/PROMPTS-PLAN-MODE.md).

## Como rodar local

    python -m venv venv
    venv\Scripts\activate          # Windows
    pip install -r requirements.txt
    copy .env.example .env         # preencher com os valores reais
    python -m pytest tests/ -q

Depois de preencher `DATABASE_PATH` e `GEMINI_API_KEY` no `.env`, rodar o
pipeline de verdade contra as fontes reais:

    python -m scripts.coletar_tudo   # camada 1: grava itens_brutos
    python -m scripts.rodar_triagem  # camadas 3+4: dedup + triagem, grava decisoes

Se algum item ficou com `status='erro'` por falha transitória (ex.: cota do
LLM esgotada), reprocessar sem repetir a coleta:

    python -m scripts.rodar_triagem --reprocessar-erros

Projeto em construção, uma etapa por vez — boletim e envio ainda não
implementados nesta fase.
