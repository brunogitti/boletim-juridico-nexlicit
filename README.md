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

Projeto em construção, uma etapa por vez — sem coletor, banco ou envio
funcionando ainda nesta fase inicial.
