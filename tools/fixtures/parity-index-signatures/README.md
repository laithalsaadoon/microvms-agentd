# Index-signature declarations

`typedoc.json` retains the `children` from TypeDoc 0.28.20's serialization of
`index.d.ts`, using TypeScript 5.9.3 and `tsconfig.json`. The project-level symbol
map and file registry are omitted because the collector does not read them and
the symbol map includes absolute source paths. Declaration subtrees are unchanged.

To regenerate from the repository root:

```bash
npx --yes --package typedoc@0.28.20 --package typescript@5.9.3 typedoc \
  --entryPoints tools/fixtures/parity-index-signatures/index.d.ts \
  --tsconfig tools/fixtures/parity-index-signatures/tsconfig.json \
  --name index-signatures --readme none \
  --json tools/fixtures/parity-index-signatures/typedoc.json
python - <<'PY'
import json
from pathlib import Path

path = Path("tools/fixtures/parity-index-signatures/typedoc.json")
project = json.loads(path.read_text())
path.write_text(json.dumps({"children": project["children"]}, indent=2) + "\n")
PY
```
