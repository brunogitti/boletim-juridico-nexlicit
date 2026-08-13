# Boletim Jurídico NexLicit

E-mail diário (7h, horário de Brasília) com decisões e entendimentos sobre a
Lei 14.133/2021, coletados de tribunais de contas, do STJ e de fonte
especializada, triados por relevância para o fornecedor que vende ao setor
público. Se não houver nada relevante no dia, não sai e-mail.

Pipeline completo e automatizado via GitHub Actions (`.github/workflows/boletim-diario.yml`):
coleta → fatiamento → dedup → triagem → análise → montagem do e-mail → envio,
rodando todo dia às 06:37 BRT. O banco SQLite persiste entre execuções na
branch [`data`](../../tree/data), separada do histórico do código.

Arquitetura completa em [docs/ARQUITETURA.md](docs/ARQUITETURA.md). Roteiro
de sessões de desenvolvimento em [docs/PROMPTS-PLAN-MODE.md](docs/PROMPTS-PLAN-MODE.md).

## Como rodar local

    python -m venv venv
    venv\Scripts\activate          # Windows
    pip install -r requirements.txt
    copy .env.example .env         # preencher com os valores reais
    python -m pytest tests/ -q

Depois de preencher `DATABASE_PATH` e `GEMINI_API_KEY` no `.env`, rodar o
pipeline de verdade contra as fontes reais, uma camada de cada vez:

    python -m scripts.coletar_tudo   # camada 1: grava itens_brutos
    python -m scripts.rodar_triagem  # camadas 2-4: fatiamento + dedup + triagem, grava decisoes
    python -m scripts.rodar_analise  # camada 5: análise completa das decisões relevantes

Se algum item ficou com `status='erro'` por falha transitória (ex.: cota do
LLM esgotada), reprocessar sem repetir a coleta:

    python -m scripts.rodar_triagem --reprocessar-erros

Por fim, montar e enviar o e-mail (precisa também de `GMAIL_USER`,
`GMAIL_APP_PASSWORD` e `EMAIL_DESTINATARIO` no `.env`):

    python -m scripts.rodar_boletim --dry-run   # salva o HTML em arquivo, não envia nada
    python -m scripts.rodar_boletim             # envia de verdade

## Automação (GitHub Actions)

O workflow roda sozinho todo dia, e também pode ser disparado manualmente pela
aba *Actions* do repositório (`workflow_dispatch`). Secrets necessários em
*Settings → Secrets and variables → Actions*: `GEMINI_API_KEY`, `GMAIL_USER`,
`GMAIL_APP_PASSWORD`, `EMAIL_DESTINATARIO`. Se alguma etapa falhar, um e-mail
de alerta é enviado e o job aparece como falho.
