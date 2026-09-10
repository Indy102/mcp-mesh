# Private Health source catalogue

These standalone tools inventory source metadata in a Health XML export and check
an explicit selection against that archive. They produce no daily Health values,
change no mesh store, and enable no adapter or inference feature.

Use Python 3.13. Run synthetic tests from the repository root:

```sh
python -m unittest discover -s spikes/health_catalogue -p 'test_*.py' -v
```

Keep the archive and every output in a private directory **outside this checkout**.
Create that directory before running commands. Output files are created with mode
0600 and existing files are refused. The examples below use placeholder paths:

```sh
python spikes/health_catalogue/catalogue.py build \
  --archive /absolute/private/export.zip \
  --output /absolute/private/catalogue.json

python spikes/health_catalogue/verify_catalogue.py \
  --archive /absolute/private/export.zip \
  --catalogue /absolute/private/catalogue.json
```

The catalogue contains exact source names, source versions, and device-description
strings, grouped separately for steps, HRV SDNN, and all SleepAnalysis records.
It includes records with invalid or missing values/times because it is a source
inventory, not measurement normalization. Different descriptor groups may be
versions of one source, and matching names may conceal different devices. Their
hashes are private metadata and do not establish hardware identity.

Review those actual sources and choose the source scope for each requested family.
Use one coherent selection for all sleep categories. Record only the user's actual
choice in a private request using the exact structure in the
[catalogue contract](../../docs/HEALTH_CATALOGUE_CONTRACT.md#selection-request-and-validated-record).
Unchosen families may remain absent from that request. Do not auto-select by record
count or manufacture a preference to make validation pass.

Then create and independently verify the selection record:

```sh
python spikes/health_catalogue/catalogue.py bind \
  --archive /absolute/private/export.zip \
  --catalogue /absolute/private/catalogue.json \
  --request /absolute/private/selection-request.json \
  --output /absolute/private/selection-binding.json

python spikes/health_catalogue/verify_catalogue.py \
  --archive /absolute/private/export.zip \
  --catalogue /absolute/private/catalogue.json \
  --binding /absolute/private/selection-binding.json
```

The binder recomputes the catalogue from the ZIP before checking membership. The
independent checker also recomputes it, using no primary project helper, and never
repairs a supplied catalogue or binding. Their accumulation, validation, and unit
tests are independently implemented; both use Python's Expat XML parser, so this
is not verification with two different parser engines. A successful result attests archive
identity, catalogue content, and selected-group membership. It does not prove
human intent, physical device identity, Health source priority, or complete
observation. Those flags remain explicitly false in the binding.

A new export requires a new catalogue and a reviewed selection for that archive.
Do not reuse ordinal labels or assume device-description text is stable. Real
catalogues, requests, bindings, and verification results stay local; public tests
use invented data. The Health [source/overlap policy](../../docs/HEALTH_SOURCE_SELECTION_DESIGN.md)
remains separate from this metadata deliverable, with its evaluator unimplemented.

The commands exit 0 on their stated operation's success and 2 on expected failure.
Their stdout/stderr use safe summaries or fixed codes; private metadata belongs
only in the explicitly requested local output. Full contracts and limitations are
in [HEALTH_CATALOGUE_CONTRACT.md](../../docs/HEALTH_CATALOGUE_CONTRACT.md).
