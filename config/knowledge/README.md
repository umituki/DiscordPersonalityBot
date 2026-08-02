# Knowledge bundles

Files here are the **owner-provided source bundle** of patch spec 16.2. They
are the external evidence that something was public at a given time — not a
statement that YUI knows it. Whether she picked any of it up is decided by the
exposure funnel (`app/knowledge/service.py`), one item at a time.

A model is deliberately not a provider. What a model knows is evidence that a
sentence is plausible, not that it was available in 1998, and that difference is
exactly what the temporal leakage guard exists to enforce.

## Format

```yaml
knowledge:
  - statement: "携帯電話でメールをやりとりするのが一般的になっていた"
    coverage_class: environmental      # foundational | environmental |
                                       # historical_cultural | interest_driven
    available_from: "2001-01-01"       # required: when this became public
    available_until: "2012-01-01"      # optional: when it stopped being current
    topic: "通信"
    geography: "jp"                    # default: global
    language: "ja"
    stability: CHANGEABLE              # STABLE | CHANGEABLE | VOLATILE
    complexity: 0.3                    # 0-1, how hard it is to understand
    salience: 0.5                      # 0-1, how hard it was to miss
```

`.json` files with the same shape work too.

`available_from` is required. A claim without one cannot be checked against a
past moment, and an unverifiable claim is how hindsight gets in.

Reading is strict: a malformed entry raises rather than being skipped, because
a knowledge base that silently drops half its rows is worse than one that
refuses to load.

## Coverage

`CoveragePlanner` asks for every coverage class in every window of the simulated
period. A window with no candidates is recorded as a gap, and the FIRST BOOT
knowledge health audit refuses a multi-year Genesis whose knowledge layer is
empty (patch spec 16.1, 17.3). Filling that gap means adding bundle files, not
lowering the audit.

`starter.yaml` is a small, deliberately generic example so the pipeline can be
exercised end to end. It is not a substitute for a real bundle for the period
you are simulating.
