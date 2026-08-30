# Migrações de Banco de Dados

Sistema de migrações versionadas próprio e sem dependências externas
(`app/persistence/migrations.py`), criado na correção v1.3 #1 depois que a
auditoria confirmou que `Base.metadata.create_all()` cria tabelas ausentes
mas nunca altera uma tabela já existente — um banco criado por uma versão
anterior do app derrubava a versão nova com
`sqlite3.OperationalError: no such column: system_state.clock_out_of_sync`.

## Versão inicial

`v0` — esquema do primeiro commit público da Fase 1 (`66f1a17`). Sem
`system_state.state_ambiguous`, sem `orders.is_close`, `orders.stop_loss`
obrigatório (`NOT NULL`), sem índice único em `candles`.

## Migrations existentes

| Versão | De → Para | O que muda |
|---|---|---|
| `1` | v0 → v1 (correção v1.1) | Adiciona `system_state.state_ambiguous`; recria a tabela `orders` adicionando `is_close` e tornando `stop_loss` opcional (ordens de fechamento não têm stop-loss). |
| `2` | v1 → v2 (correção v1.2) | Adiciona `system_state.clock_out_of_sync`; remove duplicatas históricas em `candles` (mantendo o registro de menor `id` — o mais antigo inserido — como canônico) e cria o índice único `uq_candle_symbol_timeframe_open_time`. |

`CURRENT_SCHEMA_VERSION = 2` (constante em `app/persistence/migrations.py`),
sempre igual à versão da migration mais recente da lista `MIGRATIONS`.

## Quando as migrations rodam

`app/persistence/db.py::init_db(engine)` chama
`app/persistence/migrations.py::run_migrations(engine)` — executado
automaticamente toda vez que `build_orchestrator()` inicializa (ou seja, a
cada start do processo, antes de qualquer consulta ORM). Não é necessário
nenhum passo manual em operação normal.

## Comportamento por cenário

- **Banco novo (vazio)**: cria todas as tabelas já no esquema atual
  (`Base.metadata.create_all()`) e apenas *registra* (stamp) as migrations
  como satisfeitas — não executa os `ALTER TABLE`, pois o formato final já
  existe desde a criação.
- **Banco v0 (baseline)**: aplica as migrations 1 e 2, em ordem, dentro de
  uma única transação.
- **Banco v1 (pós correção v1.1)**: detecta que a migration 1 já está
  satisfeita (inspeciona colunas existentes via `PRAGMA table_info`),
  registra-a como aplicada sem executá-la de novo, e roda apenas a
  migration 2.
- **Banco já na v2**: nenhuma alteração; idempotente.

## Transacionalidade e falha segura (correção v1.4 #1)

Toda a execução de `run_migrations()` ocorre dentro de **uma única
transação** (`engine.begin()`). Se qualquer migration lançar exceção, a
transação inteira é revertida — nenhuma migration parcial é registrada em
`schema_migrations`, e nenhuma alteração de esquema ou dado fica
"pela metade". A exceção é reembrulhada em `MigrationError`, com mensagem em
português explicando em qual versão o banco permaneceu. A aplicação não
inicia (o erro propaga até `build_orchestrator()`), em vez de seguir
silenciosamente contra um esquema desatualizado.

**Detalhe crítico do driver SQLite**: a biblioteca padrão `sqlite3` do
Python, por padrão, não entrega DDL (`ALTER TABLE`, `CREATE INDEX`,
`CREATE TABLE`) realmente transacional através do SQLAlchemy — o driver
emite seu próprio `COMMIT` implícito ao redor dessas instruções,
independente de uma transação SQLAlchemy estar aberta. Uma auditoria
adversarial reproduziu exatamente isso: uma migration fazia um `ALTER
TABLE` real e, em seguida, lançava exceção; o `ALTER` permanecia no banco
mesmo com `MigrationError` levantado. A correção (`app/persistence/db.py::
make_engine()`) desliga o gerenciamento de transação implícito do driver
(`isolation_level = None`) e faz o próprio SQLAlchemy emitir um `BEGIN`
explícito a cada transação — inclusive para DDL. Com isso, `engine.begin()`
engloba `CREATE TABLE`/`ALTER TABLE`/`CREATE INDEX` exatamente como
qualquer instrução DML, e uma falha posterior reverte tudo de verdade.
Prova adversarial reproduzindo o cenário exato do auditor (ALTER real,
depois falha, checagem de esquema E dados completos antes/depois):
`tests/test_migrations.py::test_alter_table_add_column_is_rolled_back_on_later_failure`.

## Divergência de esquema (correção v1.4 #3)

`schema_migrations` registrar uma versão como aplicada não é, por si só,
confiável — a cada execução, `run_migrations()` re-valida TODOS os
invariantes estruturais de cada versão já registrada (não apenas uma coluna
sentinela) contra o esquema real:

- **v1**: `system_state.state_ambiguous` existe; `orders.is_close` existe;
  `orders.stop_loss` aceita `NULL` (checado via `PRAGMA table_info`, não só
  presença da coluna).
- **v2**: `system_state.clock_out_of_sync` existe; existe algum índice
  único sobre `candles(symbol, timeframe, open_time)` — identificado pela
  **estrutura** (colunas cobertas), nunca por um nome fixo, então um índice
  criado por outra ferramenta com nome diferente ainda conta.

Se uma versão registrada não satisfizer integralmente seus invariantes, a
aplicação **para com `SchemaDivergenceError`**, em português, e não altera
nada automaticamente — reparo automático de um esquema já divergente foi
considerado arriscado demais (poderia mascarar corrupção real). Nesse caso,
inspecione manualmente o banco (`PRAGMA table_info`/`PRAGMA index_list`) e
decida entre reparar manualmente o esquema para bater com a versão
registrada, ou restaurar de um backup.

## Deduplicação de candles (migration 2)

Estratégia determinística e documentada: para cada grupo
`(symbol, timeframe, open_time)` com mais de uma linha, mantém apenas a
linha de **menor `id`** (a primeira que este app já inseriu para aquela
combinação) e apaga as demais, antes de criar o índice único. Coberto por
`tests/test_migrations.py::test_duplicate_candles_are_deterministically_deduplicated_before_unique_index`.

## Comando de verificação

```bash
python -m app.persistence.migrations sqlite:///./agente_trader.db
```

Imprime a versão atual do esquema e aplica qualquer migration pendente
(idempotente — pode ser executado quantas vezes for necessário).

Para apenas consultar a versão sem migrar, em Python:

```python
from app.persistence.db import make_engine
from app.persistence.migrations import current_schema_version

engine = make_engine("sqlite:///./agente_trader.db")
print(current_schema_version(engine))
```

## Backup antes de migrar

Como o arquivo é SQLite local, basta copiar o arquivo antes de iniciar a
versão nova:

```bash
cp agente_trader.db agente_trader.db.backup-$(date +%Y%m%d%H%M%S)
```

## Recuperação após falha

Se `run_migrations()` levantar `MigrationError`, o banco original permanece
intacto (a transação foi revertida) — não é necessário restaurar backup
apenas por causa da tentativa falha. Para investigar:

1. Ler a mensagem de erro (`MigrationError`), que inclui a causa original
   encadeada (`raise ... from exc`) e a versão em que o banco permaneceu.
2. Rodar `python -m app.persistence.migrations <DATABASE_URL>` novamente
   após corrigir a causa raiz (ex.: espaço em disco, permissão de arquivo).
3. Se o banco precisar ser restaurado de qualquer forma, copiar o arquivo de
   backup de volta para o caminho de `DATABASE_URL` antes de tentar migrar
   de novo.

Nunca apagar nem recriar o banco como estratégia de upgrade.
