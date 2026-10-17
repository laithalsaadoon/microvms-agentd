// SPDX-License-Identifier: Apache-2.0
// TypeDoc describes module exports, but omits ambient global augmentations.
// Read those with the same pinned TypeScript compiler that TypeDoc uses.
const fs = require('node:fs');
const ts = require(process.argv[2]);
const filename = process.argv[3];
const source = ts.createSourceFile(filename, fs.readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true);
if (source.parseDiagnostics.length) throw new Error('TypeScript declarations could not be parsed');
const printer = ts.createPrinter({ removeComments: true });
const globals = {};
const exportedNames = [];

function ambient(node, prefix) {
  if (ts.isModuleDeclaration(node)) {
    const name = prefix + node.name.text + '.';
    if (ts.isModuleBlock(node.body)) {
      for (const member of node.body.statements) ambient(member, name);
    } else if (node.body) {
      ambient(node.body, name);
    }
    return;
  }
  const names = ts.isVariableStatement(node)
    ? node.declarationList.declarations.map(item => item.name)
    : [node.name];
  for (const name of names) {
    if (!name || !ts.isIdentifier(name)) throw new Error('Unsupported ambient declaration');
    const key = prefix + name.text;
    const declaration = printer.printNode(ts.EmitHint.Unspecified, node, source);
    // Repeated ambient interfaces merge, so every declaration participates.
    (globals[key] ??= []).push(declaration);
  }
}

for (const node of source.statements) {
  if (ts.isModuleDeclaration(node) && (node.flags & ts.NodeFlags.GlobalAugmentation)) {
    ambient(node, '');
  } else if (ts.isExportDeclaration(node)) {
    if (!node.exportClause || !ts.isNamedExports(node.exportClause)) {
      throw new Error('Export-star declarations need an explicit inventory reader');
    }
    for (const item of node.exportClause.elements) exportedNames.push(item.name.text);
  } else if (ts.getModifiers(node)?.some(modifier => modifier.kind === ts.SyntaxKind.ExportKeyword)) {
    if (ts.isVariableStatement(node)) {
      for (const item of node.declarationList.declarations) {
        if (!ts.isIdentifier(item.name)) throw new Error('Destructured exports need an explicit inventory reader');
        exportedNames.push(item.name.text);
      }
    } else if (node.name && ts.isIdentifier(node.name)) {
      exportedNames.push(node.name.text);
    } else {
      throw new Error('Unnamed exports need an explicit inventory reader');
    }
  }
}
process.stdout.write(JSON.stringify({ globals, exports: exportedNames }));
