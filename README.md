# fast-translate

Biblioteca Python portátil para tradução offline (desempenho rápido em CPU):

- `en -> pt-BR`
- `pt-BR -> en`

Inclui:

- Modelos baked-in no pacote (`en-pt-tiny` e `pt-en-tiny`)
- Pós-processamento focado em pt-BR (corrige traços pt-PT)
- Runtime nativo com `translateLocally` via Native Messaging (`-p`)
- Fallback cross-platform: binário empacotado, PATH ou auto-download do GitHub Releases

## Como o projeto foi desenvolvido

O projeto foi construído com foco em uma ideia simples: usar o motor do `translateLocally` (muito rápido em CPU) como núcleo de inferência local e aplicar pós-processamento orientado por erros reais para elevar qualidade.

Fluxo usado:

1. Tradução em lote com `translateLocally` para os dois sentidos (`en->pt-BR` e `pt-BR->en`).
2. Avaliação automática + amostragem manual de erros residuais.
3. Uso de agentes de IA para agrupar padrões de erro recorrentes e propor regras objetivas de correção.
4. Implementação das regras no pós-processamento (regex + normalizações léxicas/contextuais).
5. Reavaliação com testes, ajuste iterativo e hardening de runtime cross-platform.

Datasets utilizados no processo:

- Hugging Face: `orion-research/little-stories-en_US-pt_BR`
  - usado em amostras e depois em processamento completo
  - volume processado no ciclo completo: `122.904` registros
- Arquivo local: `instruct-reasoning-dataset.parquet`
  - usado para aumentar diversidade linguística e detectar erros fora do domínio de histórias curtas
  - serviu para expandir regras de pós-processamento nas duas direções

## Instalação

```bash
pip install fast-translate
```

## Uso rápido

```python
from fast_translate import Translator

tr = Translator()
print(tr.translate("How are you today?", direction="en-pt"))
print(tr.translate("Como você está hoje?", direction="pt-en"))
tr.close()
```

## Variáveis de ambiente

- `FAST_TRANSLATE_BINARY`: caminho explícito do executável `translateLocally`
- `FAST_TRANSLATE_CACHE_SIZE`: tamanho do cache LRU (default `64`)
- `FAST_TRANSLATE_CACHE_MAX_ENTRY_CHARS`: tamanho máximo por item de cache (default `512`)
- `FAST_TRANSLATE_TRIM_EVERY_N_CALLS`: frequência de `malloc_trim` (default `8`)
- `FAST_TRANSLATE_KEEP_WARM_INTERVAL_S`: intervalo de warmup (default `300`)
- `FAST_TRANSLATE_AUTO_DOWNLOAD`: `1`/`0` para auto-download de binário (default `1`)
- `FAST_TRANSLATE_VERBOSE`: `1` para logs detalhados de resolução/download/bootstrap (default `0`)

## Desempenho em CPU

Métricas medidas em Linux (`AMD Ryzen 7 3700X`, `16 vCPUs`, Python `3.12.7`) com benchmark local e cache praticamente desabilitado (`cache_size=1`, entradas únicas).

- Startup (`Translator()`): `~0.23s`
- `en -> pt`:
  - Latência p50: `4.12 ms`
  - Latência p95: `81.64 ms`
  - Throughput: `101.48 sentenças/s` (`4916 chars/s`)
- `pt -> en`:
  - Latência p50: `3.35 ms`
  - Latência p95: `80.60 ms`
  - Throughput: `102.86 sentenças/s` (`5510 chars/s`)
- RAM (processo Python, `ru_maxrss`):
  - Antes: `31.59 MB`
  - Após init: `32.09 MB`
  - Delta de init: `+0.50 MB`

Com cache ativo (default), workloads com repetição tendem a ficar substancialmente mais rápidos.
