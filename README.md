# fast-translate

Biblioteca Python portátil para tradução offline:

- `en -> pt-BR`
- `pt -> en`

Inclui:

- Modelos baked-in no pacote (`en-pt-tiny` e `pt-en-tiny`)
- Pós-processamento focado em pt-BR (corrige traços pt-PT)
- Runtime nativo com `translateLocally` via Native Messaging (`-p`)
- Fallback cross-platform: binário empacotado, PATH ou auto-download do GitHub Releases

## Instalação

```bash
pip install fast-translate
```

## Uso rápido

```python
from tlptbr_translate import Translator

tr = Translator()
print(tr.translate("How are you today?", direction="en-pt"))
print(tr.translate("Como você está hoje?", direction="pt-en"))
tr.close()
```

## Variáveis de ambiente

- `TLPTBR_BINARY`: caminho explícito do executável `translateLocally`
- `TLPTBR_CACHE_SIZE`: tamanho do cache LRU (default `64`)
- `TLPTBR_CACHE_MAX_ENTRY_CHARS`: tamanho máximo por item de cache (default `512`)
- `TLPTBR_TRIM_EVERY_N_CALLS`: frequência de `malloc_trim` (default `8`)
- `TLPTBR_KEEP_WARM_INTERVAL_S`: intervalo de warmup (default `300`)
- `TLPTBR_AUTO_DOWNLOAD`: `1`/`0` para auto-download de binário (default `1`)

## Nota sobre portabilidade

Os modelos são baked-in. O binário `translateLocally` pode ser:

1. empacotado no wheel da plataforma,
2. encontrado no PATH,
3. baixado automaticamente do GitHub Releases (quando habilitado).

## Publicação com GitHub Actions

Workflow: `.github/workflows/build_publish.yml`

- `ci` roda testes em Linux, macOS e Windows.
- `package` gera `sdist` + `wheel`.
- `publish` publica no PyPI em tags `v*`.

Configure o segredo do repositório:

- `PYPI_API_TOKEN`
