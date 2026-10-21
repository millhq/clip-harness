# Development

```sh
pip install -e '.[dev]'
python3 -m pytest        # 387 tests: parser, gate, contract, lifecycle
```

Design rationale, in one line: correctness and contracts live in
code, behaviour that must always happen lives in the core, and
everything else is a hook.
